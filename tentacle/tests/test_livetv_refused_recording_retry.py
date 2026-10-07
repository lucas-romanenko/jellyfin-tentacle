"""A recording whose tuner open failed in its pre-padding is still a
recording when Jellyfin retries it.

Run from the tentacle/ directory:  python -m unittest discover -s tests

Jellyfin 10.11.8 retries a recording whose tuner open failed every minute
(RecordingsManager: Status New, PrePaddingSeconds 0, StartDate now + 60 s,
at most 10 times, then the timer is deleted), and at each fire resets
StartDate/EndDate to the programme's. In the pre-padding a retry therefore
reads "starts in 14 minutes, no padding": not imminent, so it was ranked as
a viewer -- refused at capacity (it could not take a viewer's slot) and under
recording protection, until Jellyfin deleted the timer before the programme
began. A timer Tentacle has seen fire and that is New again with no
pre-padding is such a retry.
"""
import asyncio
import unittest
from datetime import datetime, timedelta
from unittest import mock

from routers import livetv


def _iso(d):
    return d.strftime("%Y-%m-%dT%H:%M:%S.0000000Z")


class _Base(unittest.TestCase):
    def setUp(self):
        self.fired = getattr(livetv, "_fired_timers", {})   # {} on a build without it
        self.starts = getattr(livetv, "_fired_starts", {})
        for d in (self.fired, self.starts):
            d.clear()
            self.addCleanup(d.clear)
        self.now = datetime.utcnow().replace(microsecond=0)

    def ask(self, *timers):
        """What the lookup says about the timers' channel(s), at self.now."""
        class _Now(datetime):
            @classmethod
            def utcnow(cls, _t=self.now):
                return _t
        with mock.patch.object(livetv, "_jellyfin_timers", lambda u, k: {"Items": list(timers)}), \
                mock.patch.object(livetv, "datetime", _Now):
            return livetv._recording_stream_ids_from_jellyfin("http://jf.test", "k")

    def timer(self, start_in, pre, tid="t-1002", end_in=None, post=5400, status="New", ext="hdhr_1002"):
        t = {"ExternalChannelId": ext, "Status": status,
             "StartDate": _iso(self.now + timedelta(seconds=start_in)), "PrePaddingSeconds": pre,
             "EndDate": _iso(self.now + timedelta(seconds=(end_in if end_in is not None else start_in + 3600))),
             "PostPaddingSeconds": post}
        if tid is not None:
            t["Id"] = tid
        return t


class JellyfinsRetryCycle(_Base):
    """The programme starts in 15 minutes; 15 minutes of pre-padding."""

    def test_every_step_of_the_retry_cycle_is_a_recording(self):
        prog = 900
        self.assertEqual({"1002"}, self.ask(self.timer(prog, 900)), "first fire (pre-padding starts)")
        self.now += timedelta(seconds=5)
        self.assertEqual({"1002"}, self.ask(self.timer(60, 0, end_in=prog - 5 + 3600)),
                         "refused: New, StartDate now+60, no pre-padding")
        self.now += timedelta(seconds=60)
        self.assertEqual({"1002"}, self.ask(self.timer(prog - 65, 0)),
                         "the retry fired: StartDate reset to the programme -- was a viewer")
        self.now += timedelta(seconds=prog - 65 - 90)
        self.assertEqual({"1002"}, self.ask(self.timer(90, 0)), "90 s before the programme")

    def test_after_a_restart_it_is_as_before(self):
        self.ask(self.timer(900, 900))
        self.fired.clear()                       # Tentacle restarted
        self.assertEqual(set(), self.ask(self.timer(840, 0)))


class NothingElseCounts(_Base):
    def test_a_timer_that_never_fired_is_not_remembered(self):
        # seen 60 s before it fires (imminent, not due), then the guide moves it an hour
        self.assertEqual({"1002"}, self.ask(self.timer(960, 900)))
        self.assertEqual(set(), self.ask(self.timer(960 + 3600, 900)))
        # seen 60 s before it fires, then its pre-padding trimmed to 0 (starts in 16 min)
        self.assertEqual({"1002"}, self.ask(self.timer(960, 900)))
        self.assertEqual(set(), self.ask(self.timer(960, 0)))
        self.assertEqual({}, self.fired)

    def test_a_guide_refresh_during_the_retries_stops_the_count_when_the_programme_moved(self):
        """Jellyfin's guide refresh copies the (moved) programme dates onto a
        waiting timer and keeps PrePadding 0; it opens nothing until then."""
        self.assertEqual({"1002"}, self.ask(self.timer(900, 900)))       # fired, programme in 15 min
        self.now += timedelta(seconds=65)
        self.assertEqual({"1002"}, self.ask(self.timer(835, 0)), "the retry")
        self.assertEqual(set(), self.ask(self.timer(835 + 3600, 0)), "programme moved an hour later")
        self.assertEqual({"1002"}, self.ask(self.timer(835 + 60, 0)), "moved within the imminent window: still")

    def test_a_recording_seen_only_in_progress_is_remembered(self):
        """No lookup caught its fire; it was seen recording, then failed in
        its pre-padding and is retried."""
        self.assertEqual({"1002"}, self.ask(self.timer(600, 900, status="InProgress")))
        self.now += timedelta(seconds=65)
        self.assertEqual({"1002"}, self.ask(self.timer(535, 0)))

    def test_an_entry_past_its_end_is_gone(self):
        self.ask(self.timer(0, 0, end_in=600, post=60))
        self.now += timedelta(seconds=660)
        self.ask(self.timer(-660, 0, end_in=-60, post=60))
        self.assertEqual(({}, {}), (self.fired, self.starts))

    def test_a_fired_timer_with_its_padding_back_is_ranked_by_its_dates(self):
        self.ask(self.timer(900, 900))
        self.assertEqual(set(), self.ask(self.timer(900 + 7200, 900)), "not a retry: padding restored")

    def test_timers_without_an_id_or_an_end_are_never_remembered(self):
        self.assertEqual({"1002"}, self.ask(self.timer(0, 0, tid=None)))
        t = self.timer(0, 0)
        del t["EndDate"]
        self.assertEqual({"1002"}, self.ask(t))
        self.assertEqual({}, self.fired)
        self.assertEqual(set(), self.ask(self.timer(7200, 0, tid=None)))

    def test_a_timer_jellyfin_deleted_is_forgotten(self):
        self.ask(self.timer(900, 900))
        self.ask(self.timer(3600, 0, tid="other", ext="hdhr_7"))   # an answer without it
        self.assertNotIn("t-1002", self.fired)

    def test_it_ends_with_the_recording(self):
        self.ask(self.timer(0, 0, end_in=600, post=60))
        self.assertIn("t-1002", self.fired)
        self.now += timedelta(seconds=660)                        # EndDate + post-padding
        self.assertEqual(set(), self.ask(self.timer(-660, 0, end_in=-60, post=60)))
        self.assertEqual({}, self.fired)

    def test_a_failed_lookup_changes_nothing(self):
        self.ask(self.timer(900, 900))
        before = dict(self.fired)

        def boom(u, k):
            raise RuntimeError("Jellyfin answered HTTP 500")
        with mock.patch.object(livetv, "_jellyfin_timers", boom), self.assertRaises(RuntimeError):
            livetv._recording_stream_ids_from_jellyfin("http://jf.test", "k")
        self.assertEqual(before, self.fired)


class TheRetryRanksAsARecording(_Base, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _Base.setUp(self)
        self.saved = dict(livetv._stream_slots.leases)
        self.saved_status = dict(livetv._stream_status)
        livetv._stream_slots.leases.clear()
        livetv._stream_status.clear()
        self.addCleanup(lambda: (livetv._stream_slots.leases.clear(), livetv._stream_slots.leases.update(self.saved),
                                 livetv._stream_status.clear(), livetv._stream_status.update(self.saved_status)))

    async def test_the_retry_takes_a_viewers_slot_at_capacity(self):
        s = livetv._StreamSlots()
        viewer = await s.acquire_lease(1, 0.01, "live", "channel:3", stream_key="1003")
        stopped = []
        viewer.on_preempt = lambda: stopped.append("viewer")
        self.ask(self.timer(900, 900))                            # fired, then refused
        self.now += timedelta(seconds=65)
        kind = "recording" if "1002" in self.ask(self.timer(835, 0)) else "live"
        self.assertEqual("recording", kind)
        self.assertIsNotNone(await s.acquire_lease(1, 0.01, kind, "channel:2", stream_key="1002"))
        self.assertEqual(["viewer"], stopped)

    async def test_a_working_retry_is_a_recording_rival_under_184(self):
        """What changes: a programme recording on the same account yields to
        a retry that delivers, as it does to the recording's first attempt."""
        slots = livetv._stream_slots
        a = slots._grant("recording", "channel:1", "1001")
        b = slots._grant("live", "channel:2", "1002")
        for lease, cid in ((a, 1), (b, 2)):
            lease.provider_id, lease.channel_id = 5, cid
        now = asyncio.get_running_loop().time()
        b.started = now - 60
        livetv._stream_status[2] = {"state": "streaming", "since": now, "opened_at": now,
                                    "last_error": None, "last_ok": now - 1}
        self.ask(self.timer(900, 900))
        self.now += timedelta(seconds=65)
        keys = self.ask(self.timer(835, 0), self.timer(-600, 0, tid="a", ext="hdhr_1001", status="InProgress"))
        with mock.patch.dict(livetv._recording_cache, {"ok_at": now}):
            slots.sync_recordings(keys)
            self.assertEqual("recording", b.kind)
            self.assertIs(b, livetv._rival_delivering(a))


if __name__ == "__main__":
    unittest.main()
