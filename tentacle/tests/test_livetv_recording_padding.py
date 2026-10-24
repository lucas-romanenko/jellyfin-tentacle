"""At the connection limit, a recording whose programme is starting takes the
slot of a recording that is only in its post-padding (#300).

Run from the tentacle/ directory:  python -m unittest tests.test_livetv_recording_padding

Jellyfin pads recordings (5 min before / 30 after, sports 15 / 90). On an
account that carries one stream (limit 1), recording A held the slot for its
whole post-padding, and programme B on another channel was refused until A's
padding ended -- often after B's programme. Now the timer's phase decides:
inside the programme (StartDate..EndDate, 10 s grace), pre- or post-padding.
Only post-padding gives way, only to a recording whose programme has not
ended, only on a Jellyfin answer issued after the newcomer began to open.
"""
import asyncio
import unittest
from datetime import datetime, timedelta
from unittest import mock


def _fmt(d):
    return d.strftime("%Y-%m-%dT%H:%M:%S.0000000Z")


class Phases(unittest.TestCase):
    def setUp(self):
        from routers import livetv
        self.livetv = livetv
        livetv._fired_timers.clear()
        livetv._fired_starts.clear()

    def test_each_recording_is_in_its_programme_or_its_padding(self):
        now = datetime.utcnow()
        timers = {"Items": [
            # inside its programme
            {"Status": "InProgress", "ExternalChannelId": "hdhr_1", "StartDate": _fmt(now - timedelta(minutes=20)),
             "EndDate": _fmt(now + timedelta(minutes=40)), "PrePaddingSeconds": 300, "PostPaddingSeconds": 1800},
            # its programme ended 5 min ago: post-padding
            {"Status": "InProgress", "ExternalChannelId": "hdhr_2", "StartDate": _fmt(now - timedelta(hours=1)),
             "EndDate": _fmt(now - timedelta(minutes=5)), "PostPaddingSeconds": 1800},
            # starts in 10 min, 15 min pre-padding: pre-padding
            {"Status": "InProgress", "ExternalChannelId": "hdhr_3", "StartDate": _fmt(now + timedelta(minutes=10)),
             "EndDate": _fmt(now + timedelta(hours=2)), "PrePaddingSeconds": 900},
            # ended 5 s ago: still inside (grace)
            {"Status": "InProgress", "ExternalChannelId": "hdhr_4", "StartDate": _fmt(now - timedelta(hours=1)),
             "EndDate": _fmt(now - timedelta(seconds=5)), "PostPaddingSeconds": 600},
            # back to back on one channel: the one inside its programme wins
            {"Status": "InProgress", "ExternalChannelId": "hdhr_5", "StartDate": _fmt(now - timedelta(hours=2)),
             "EndDate": _fmt(now - timedelta(minutes=2)), "PostPaddingSeconds": 1800},
            {"Status": "New", "ExternalChannelId": "hdhr_5", "StartDate": _fmt(now - timedelta(minutes=1)),
             "EndDate": _fmt(now + timedelta(hours=1))},
        ]}
        with mock.patch.object(self.livetv, "_jellyfin_timers", lambda u, k: timers):
            answer = self.livetv._recording_stream_ids_from_jellyfin("http://jf.test", "k")
        self.assertEqual({"1", "2", "3", "4", "5"}, answer)
        self.assertEqual({"1": "programme", "2": "post", "3": "pre", "4": "programme", "5": "programme"},
                         {k: v[0] for k, v in answer.phases.items()})
        self.assertEqual(datetime.strptime(_fmt(now - timedelta(minutes=5))[:19], "%Y-%m-%dT%H:%M:%S"),
                         answer.phases["2"][1])


class Handover(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from routers import livetv
        self.livetv = livetv
        self.cache = dict(livetv._recording_cache)
        self.addCleanup(lambda: (livetv._recording_cache.clear(), livetv._recording_cache.update(self.cache)))

    def _current(self, issued=None):
        """A lookup answer that is current, issued at `issued` (now)."""
        now = asyncio.get_running_loop().time()
        self.livetv._recording_cache.update(at=now, pending=None, failures=0, retry_at=-1e9,
                                            answer_issued_at=now if issued is None else issued)

    async def _recording(self, s, limit, key, phase, end_minutes):
        lease = await s.acquire_lease(limit, 0.01, "recording", f"channel:{key}", stream_key=key)
        lease.phase, lease.programme_end = phase, datetime.utcnow() + timedelta(minutes=end_minutes)
        stopped = []
        lease.on_preempt = lambda: stopped.append(key)
        return lease, stopped

    async def test_a_starting_recording_takes_the_slot_of_one_in_post_padding(self):
        s = self.livetv._StreamSlots()
        a, stopped = await self._recording(s, 1, "1", "post", -1)
        began = asyncio.get_running_loop().time()
        self._current()
        with self.assertLogs("routers.livetv", "WARNING") as logs:
            b = await s.acquire_lease(1, 0.01, "recording", "channel:2", stream_key="2",
                                      phase="programme", fresh_since=began)
        self.assertIsNotNone(b)
        self.assertEqual(["1"], stopped)
        self.assertEqual({"channel:2"}, {l.owner for l in s.leases.values()})
        self.assertEqual(1, s.padding_handovers)
        self.assertIn("its programme is over; only its post-padding is lost", logs.output[0])

    async def test_one_in_pre_padding_may_take_it_too(self):
        s = self.livetv._StreamSlots()
        _, stopped = await self._recording(s, 1, "1", "post", -1)
        began = asyncio.get_running_loop().time()
        self._current()
        self.assertIsNotNone(await s.acquire_lease(1, 0.01, "recording", "channel:2", stream_key="2",
                                                   phase="pre", fresh_since=began))
        self.assertEqual(["1"], stopped)

    async def test_pre_padding_and_programmes_are_never_taken(self):
        for phase in ("pre", "programme"):
            with self.subTest(phase=phase):
                s = self.livetv._StreamSlots()
                _, stopped = await self._recording(s, 1, "1", phase, 30)
                began = asyncio.get_running_loop().time()
                self._current()
                self.assertIsNone(await s.acquire_lease(1, 0.01, "recording", "channel:2", stream_key="2",
                                                        phase="programme", fresh_since=began))
                self.assertEqual([], stopped)

    async def test_a_recording_in_post_padding_takes_nothing_itself(self):
        s = self.livetv._StreamSlots()
        _, stopped = await self._recording(s, 1, "1", "post", -1)
        began = asyncio.get_running_loop().time()
        self._current()
        self.assertIsNone(await s.acquire_lease(1, 0.01, "recording", "channel:2", stream_key="2",
                                                phase="post", fresh_since=began))
        self.assertEqual([], stopped)

    async def test_only_on_an_answer_issued_after_the_newcomer_began_to_open(self):
        s = self.livetv._StreamSlots()
        _, stopped = await self._recording(s, 1, "1", "post", -1)
        old = asyncio.get_running_loop().time()
        self._current(issued=old - 1)
        self.assertIsNone(await s.acquire_lease(1, 0.01, "recording", "channel:2", stream_key="2",
                                                phase="programme", fresh_since=old))
        self.assertEqual([], stopped)
        # Jellyfin cannot be asked: every recording counts as inside its programme.
        self.livetv._recording_cache.update(failures=1)
        self.assertIsNone(await s.acquire_lease(1, 0.01, "recording", "channel:3", stream_key="3",
                                                phase="programme", fresh_since=None))
        self.assertEqual([], stopped)

    async def test_the_recording_whose_programme_ended_first_gives_way(self):
        s = self.livetv._StreamSlots()
        _, first = await self._recording(s, 2, "1", "post", -20)
        _, second = await self._recording(s, 2, "2", "post", -5)
        began = asyncio.get_running_loop().time()
        self._current()
        await s.acquire_lease(2, 0.01, "recording", "channel:3", stream_key="3", phase="programme", fresh_since=began)
        self.assertEqual((["1"], []), (first, second))

    async def test_a_viewer_gives_way_before_any_recording(self):
        s = self.livetv._StreamSlots()
        _, rec_stopped = await self._recording(s, 2, "1", "post", -1)
        viewer = await s.acquire_lease(2, 0.01, "live", "channel:9", stream_key="9")
        viewer_stopped = []
        viewer.on_preempt = lambda: viewer_stopped.append("9")
        began = asyncio.get_running_loop().time()
        self._current()
        await s.acquire_lease(2, 0.01, "recording", "channel:2", stream_key="2", phase="programme", fresh_since=began)
        self.assertEqual((["9"], []), (viewer_stopped, rec_stopped))

    async def test_back_to_back_the_newcomer_waits_for_the_lookup_that_sees_the_padding(self):
        s = self.livetv._StreamSlots()
        a, stopped = await self._recording(s, 1, "1", "programme", 0)    # its programme ends now
        began = asyncio.get_running_loop().time()
        self._current(issued=began - 1)

        async def next_lookup():
            await asyncio.sleep(0.05)
            self._current()
            s.sync_recordings({"1"}, issued_at=self.livetv._recording_cache["answer_issued_at"],
                              phases={"1": ("post", datetime.utcnow() - timedelta(seconds=11))})
            s._wake()
        task = asyncio.create_task(next_lookup())
        b = await s.acquire_lease(1, 0.01, "recording", "channel:2", stream_key="2",
                                  phase="programme", fresh_since=began)
        await task
        self.assertIsNotNone(b, "waited past its 0.01 s for the hand-over")
        self.assertEqual(["1"], stopped)

    async def test_no_lookup_means_every_recording_is_inside_its_programme_again(self):
        s = self.livetv._StreamSlots()
        a, _ = await self._recording(s, 1, "1", "post", -1)
        s.reset_phases()
        self.assertEqual("programme", a.phase)

    async def test_sync_sets_phases_and_a_key_without_one_is_inside_its_programme(self):
        s = self.livetv._StreamSlots()
        a = await s.acquire_lease(3, 0.01, "recording", "channel:1", stream_key="1")
        b = await s.acquire_lease(3, 0.01, "recording", "channel:2", stream_key="2")
        v = await s.acquire_lease(3, 0.01, "live", "channel:3", stream_key="3")
        end = datetime.utcnow() - timedelta(minutes=3)
        self.assertEqual(1, s.sync_recordings({"1", "2"}, phases={"1": ("post", end)}))
        self.assertEqual(("post", end), (a.phase, a.programme_end))
        self.assertEqual("programme", b.phase)
        self.assertEqual("programme", v.phase)
        # without phases (an older caller): unchanged ranks, everything inside
        s.sync_recordings({"1", "2"})
        self.assertEqual("programme", a.phase)

    async def test_the_rival_rank_puts_post_padding_just_above_a_viewer(self):
        s = self.livetv._StreamSlots()
        a = await s.acquire_lease(3, 0.01, "recording", "channel:1", stream_key="1")
        pre = await s.acquire_lease(3, 0.01, "recording", "channel:2", stream_key="2")
        v = await s.acquire_lease(3, 0.01, "live", "channel:3", stream_key="3")
        a.phase, pre.phase = "post", "pre"
        rank = self.livetv._rank
        self.assertLess(rank(pre), rank(a))
        self.assertLess(rank(a), rank(v))


class WithoutPadding(unittest.IsolatedAsyncioTestCase):
    """No padding, no change: random sequences decide exactly as #150's rule
    (recordings inside their programmes) did before phases existed."""

    async def test_random_sequences_match_the_old_rule(self):
        import logging
        import random
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        from routers import livetv
        for seed in range(300):
            rnd = random.Random(seed)
            new, old = livetv._StreamSlots(), livetv._StreamSlots()
            limit = rnd.randint(1, 3)
            for step in range(12):
                kind = rnd.choice(["recording", "live", "vod"])
                key = str(rnd.randint(1, 6))
                if rnd.random() < 0.25 and new.leases:
                    lid = rnd.choice(sorted(new.leases))
                    new.release_lease(new.leases.get(lid))
                    old.release_lease(old.leases.get(lid))
                    continue
                got = await new.acquire_lease(limit, 0.0, kind, f"channel:{key}", stream_key=key,
                                              phase="programme", fresh_since=0.0)
                was = await old.acquire_lease(limit, 0.0, kind, f"channel:{key}", stream_key=key)
                self.assertEqual(was is None, got is None, f"seed {seed} step {step}")
                self.assertEqual(sorted(old.leases), sorted(new.leases), f"seed {seed} step {step}")


if __name__ == "__main__":
    unittest.main()
