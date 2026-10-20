"""Live TV from several providers (#704, Roadmap: multi-provider Live TV).

Live TV used to assume one provider: one connection limit for everything
and channel numbers taken straight from the provider's stream ids, which two
providers can share (Jellyfin then merges the two channels: it keys a
channel on hdhr_<GuideNumber>).

  * Each provider's account carries its own number of streams ("Streams at
    once" on its card); a full account takes a slot only from its own
    pulls, and never makes another provider's stream wait.
  * The first Live TV provider keeps its numbers (an install with one
    provider sees no change); every other gets an offset once, and keeps it.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import asyncio
import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb
from models.database import LiveChannel, Provider, Setting
from tmp_dirs import temp_dir


class _Db(unittest.TestCase):
    def setUp(self):
        engine = create_engine(f"sqlite:///{temp_dir(self)}/t.db", connect_args={"check_same_thread": False})
        mdb.Base.metadata.create_all(engine)
        self.addCleanup(engine.dispose)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)
        self.a = Provider(name="A", server_url="http://a.example", username="u", password="p",
                          live_tv_enabled=True)
        self.b = Provider(name="B", server_url="http://b.example", username="u", password="p",
                          live_tv_enabled=True)
        self.db.add_all([self.a, self.b])
        self.db.commit()


class ChannelNumbers(_Db):
    def setUp(self):
        super().setUp()
        self.db.add_all([
            LiveChannel(provider_id=self.a.id, name="A1", stream_id="605186", enabled=True, stream_url="x"),
            LiveChannel(provider_id=self.b.id, name="B1", stream_id="605186", enabled=True, stream_url="y"),
            LiveChannel(provider_id=self.b.id, name="B2", stream_id="77", enabled=True, stream_url="z"),
        ])
        self.db.commit()

    def test_the_first_provider_keeps_its_numbers_the_next_gets_a_range(self):
        numbers = {ch.name: n for ch, n in self._numbers()}
        self.assertEqual("605186", numbers["A1"])
        self.assertEqual("1605186", numbers["B1"])
        self.assertEqual("1000077", numbers["B2"])
        self.db.expire_all()
        self.assertEqual(0, self.db.get(Provider, self.a.id).channel_number_offset)
        self.assertEqual(1000000, self.db.get(Provider, self.b.id).channel_number_offset)

    def _numbers(self):
        import routers.livetv as lt
        chans = self.db.query(LiveChannel).order_by(LiveChannel.id).all()
        return lt._unique_numbers(self.db, chans)

    def test_kept_once_given(self):
        """A channel with a longer id arriving later does not renumber
        anything Jellyfin already knows."""
        self._numbers()
        self.db.add(LiveChannel(provider_id=self.a.id, name="A2", stream_id="12345678", enabled=True, stream_url="w"))
        self.db.commit()
        self.assertEqual("1000077", dict((ch.name, n) for ch, n in self._numbers())["B2"])

    def test_numbers_find_their_channels_again(self):
        """Jellyfin's timers say hdhr_<GuideNumber>: the recording lookup and
        a reservation map it back to the right provider's channel."""
        import routers.livetv as lt
        self._numbers()
        found = lt.channels_for_guide_numbers(self.db, ["605186", "1000077"])
        self.assertEqual({"A1", "B2"}, {c.name for c in found})

    def test_a_clash_is_left_out_not_merged(self):
        """An offset edited onto another provider's range: the second channel
        with a number is left out (and logged), never handed over twice."""
        import routers.livetv as lt
        self._numbers()
        self.b.channel_number_offset = 0
        self.db.commit()
        names = [ch.name for ch, _ in self._numbers()]
        self.assertEqual(["A1", "B2"], names)

    def test_one_provider_installs_see_no_change(self):
        import routers.livetv as lt
        self.b.live_tv_enabled = False
        self.db.query(LiveChannel).filter(LiveChannel.provider_id == self.b.id).delete()
        self.db.commit()
        self.assertEqual([("A1", "605186")], [(c.name, n) for c, n in self._numbers()])


class StreamsAtOnce(_Db):
    def test_limits(self):
        import routers.livetv as lt
        self.a.live_max_streams = 1
        self.db.commit()
        self.assertEqual(1, lt.provider_stream_limit(self.db, self.a))
        self.assertEqual(6, lt.provider_stream_limit(self.db, self.b))     # the default
        self.assertEqual(7, lt.overall_stream_limit(self.db))
        self.db.add(Setting(key="hdhr_tuner_count", value="3"))
        self.db.commit()
        self.assertEqual(7, lt._hdhr_tuner_count(self.db))

    def test_a_full_account_does_not_hold_up_another(self):
        import routers.livetv as lt

        async def go():
            s = lt._StreamSlots()
            a1 = await s.acquire_lease(7, 0.01, "live", "channel:1", provider_id=1, provider_limit=1)
            self.assertIsNotNone(a1)
            # A's one connection is in use: a second viewer on A is refused...
            self.assertIsNone(await s.acquire_lease(7, 0.01, "live", "channel:2", provider_id=1, provider_limit=1))
            # ...while B, with room of its own, is not held up
            b1 = await s.acquire_lease(7, 0.01, "live", "channel:3", provider_id=2, provider_limit=6)
            self.assertIsNotNone(b1)
            return s, a1, b1
        s, a1, b1 = asyncio.run(go())
        self.assertEqual(1, s.provider_active(1))
        self.assertEqual(1, s.provider_active(2))

    def test_a_recording_takes_only_its_own_accounts_slot(self):
        import routers.livetv as lt

        async def go():
            s = lt._StreamSlots()
            viewer_a = await s.acquire_lease(7, 0.01, "live", "channel:1", provider_id=1, provider_limit=1)
            viewer_b = await s.acquire_lease(7, 0.01, "live", "channel:3", provider_id=2, provider_limit=6)
            rec = await s.acquire_lease(7, 0.01, "recording", "channel:2", provider_id=1, provider_limit=1)
            return viewer_a, viewer_b, rec, s
        viewer_a, viewer_b, rec, s = asyncio.run(go())
        self.assertIsNotNone(rec)
        self.assertTrue(viewer_a.preempted)         # A's viewer made way
        self.assertFalse(viewer_b.preempted)        # B's was never a candidate
        self.assertEqual({rec.id, viewer_b.id}, set(s.leases))

    def test_without_providers_it_is_the_old_ceiling(self):
        import routers.livetv as lt

        async def go():
            s = lt._StreamSlots()
            first = await s.acquire_lease(1, 0.01, "live", "channel:1")
            second = await s.acquire_lease(1, 0.01, "live", "channel:2")
            return first, second
        first, second = asyncio.run(go())
        self.assertIsNotNone(first)
        self.assertIsNone(second)


class TheCardSetsThem(_Db):
    def test_put_saves_the_limit_and_offset(self):
        import routers.providers as rp
        from unittest import mock
        with mock.patch("services.provider_relink.start_relink", lambda *a: None):
            rp.update_provider(self.a.id, rp.ProviderUpdate(live_max_streams=2, channel_number_offset=5000), self.db)
        self.db.expire_all()
        a = self.db.get(Provider, self.a.id)
        self.assertEqual((2, 5000), (a.live_max_streams, a.channel_number_offset))
        with mock.patch("services.provider_relink.start_relink", lambda *a: None):
            rp.update_provider(self.a.id, rp.ProviderUpdate(live_max_streams=0), self.db)
        self.db.expire_all()
        self.assertIsNone(self.db.get(Provider, self.a.id).live_max_streams)


if __name__ == "__main__":
    unittest.main()
