"""The TV guide's muted live preview of the focused channel is one more
provider connection: GET /api/live/preview says whether one may run now.
Only with spare connections: after the preview at least one stays free, and
a recording that starts within 10 minutes (start minus pre-padding) counts
as taken already. No limit: always.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import asyncio
import unittest
from datetime import datetime, timedelta
from unittest import mock

from routers import livetv as L

NOW = datetime(2026, 10, 25, 18, 0, 0)


def window(number="101", starts_in_min=5, length_min=120):
    start = NOW + timedelta(minutes=starts_in_min)
    return (number, start, start + timedelta(minutes=length_min))


class Rule(unittest.TestCase):
    def test_no_limit_always_previews(self):
        out = L.preview_answer(0, 7, set(), [window()], NOW)
        self.assertTrue(out["previews"])
        self.assertIsNone(out["free"])

    def test_two_free_previews(self):
        out = L.preview_answer(4, 2, set(), [], NOW)
        self.assertEqual((True, 2), (out["previews"], out["free"]))

    def test_one_free_no_preview(self):
        out = L.preview_answer(4, 3, set(), [], NOW)
        self.assertEqual((False, 1), (out["previews"], out["free"]))

    def test_an_upcoming_recording_counts_as_taken(self):
        out = L.preview_answer(4, 2, set(), [window(starts_in_min=8)], NOW)
        self.assertEqual((False, 1, 1), (out["previews"], out["free"], out["recordings_soon"]))

    def test_a_recording_later_than_ten_minutes_does_not_count(self):
        out = L.preview_answer(4, 2, set(), [window(starts_in_min=11)], NOW)
        self.assertEqual((True, 2, 0), (out["previews"], out["free"], out["recordings_soon"]))

    def test_a_recording_already_pulling_is_not_counted_twice(self):
        # its channel holds a slot (it is in `active` already)
        out = L.preview_answer(4, 2, {"101"}, [window("101", starts_in_min=-30)], NOW)
        self.assertEqual((True, 2, 0), (out["previews"], out["free"], out["recordings_soon"]))

    def test_an_ended_timer_does_not_count(self):
        out = L.preview_answer(4, 2, set(), [window(starts_in_min=-200, length_min=60)], NOW)
        self.assertEqual(0, out["recordings_soon"])

    def test_two_timers_on_one_channel_take_one_connection(self):
        out = L.preview_answer(5, 1, set(), [window("7", 2), window("7", 9)], NOW)
        self.assertEqual((1, 3), (out["recordings_soon"], out["free"]))

    def test_a_running_preview_keeps_going_with_one_spare(self):
        # the caller's own preview is in `active`: one more free is enough
        self.assertTrue(L.preview_answer(4, 3, set(), [], NOW, previewing=True)["previews"])
        self.assertFalse(L.preview_answer(4, 4, set(), [], NOW, previewing=True)["previews"])

    def test_unknown_timers_mean_no_preview(self):
        out = L.preview_answer(4, 0, set(), None, NOW)
        self.assertEqual((False, False), (out["previews"], out["known"]))


class Windows(unittest.TestCase):
    def test_padding_widens_the_window_and_other_tuners_are_ignored(self):
        items = [
            {"Status": "New", "ExternalChannelId": "hdhr_101", "StartDate": "2026-10-25T18:12:00.0000000Z",
             "EndDate": "2026-10-25T20:00:00Z", "PrePaddingSeconds": 300, "PostPaddingSeconds": 600},
            {"Status": "New", "ExternalChannelId": "other_5", "StartDate": "2026-10-25T18:01:00Z",
             "EndDate": "2026-10-25T19:00:00Z"},
            {"Status": "Cancelled", "ExternalChannelId": "hdhr_102", "StartDate": "2026-10-25T18:01:00Z",
             "EndDate": "2026-10-25T19:00:00Z"},
        ]
        out = L._recording_windows(items, {})
        self.assertEqual([("101", datetime(2026, 10, 25, 18, 7), datetime(2026, 10, 25, 20, 10))], out)
        # 18:12 minus 5 min pre-padding = 18:07: within 10 minutes of 18:00
        self.assertEqual(1, L.preview_answer(4, 0, set(), out, NOW)["recordings_soon"])


class Endpoint(unittest.TestCase):
    def setUp(self):
        self._cache = dict(L._preview_timers)
        L._preview_timers.update(at=-1e9, ok_at=-1e9, windows=None)
        self._leases = dict(L._stream_slots.leases)
        L._stream_slots.leases.clear()
        self._reserved = dict(L._reserved_channels)
        L._reserved_channels.clear()

    def tearDown(self):
        L._preview_timers.clear()
        L._preview_timers.update(self._cache)
        L._stream_slots.leases.clear()
        L._stream_slots.leases.update(self._leases)
        L._reserved_channels.clear()
        L._reserved_channels.update(self._reserved)

    def _call(self, timers=None, error=None, limit=4):
        values = {"jellyfin_url": "http://jf.test", "jellyfin_api_key": "k"}
        gs = lambda _db, k, d="": values.get(k, d)
        jt = mock.Mock(return_value=timers, side_effect=error)
        with mock.patch.object(L, "get_setting", gs), mock.patch.object(L, "_jellyfin_timers", jt), \
                mock.patch.object(L, "overall_stream_limit", return_value=limit):
            out = asyncio.run(L.live_preview(previewing=False, user=object(), db=mock.Mock()))
        return out, jt

    def test_a_timer_starting_soon_stops_previews(self):
        start = (datetime.utcnow() + timedelta(minutes=4)).strftime("%Y-%m-%dT%H:%M:%SZ")
        end = (datetime.utcnow() + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        timers = {"Items": [{"Status": "New", "ExternalChannelId": "hdhr_9", "StartDate": start, "EndDate": end}]}
        out, _ = self._call(timers, limit=2)
        self.assertEqual((False, 1, 1), (out["previews"], out["free"], out["recordings_soon"]))

    def test_nothing_booked_and_room_previews(self):
        out, _ = self._call({"Items": []})
        self.assertEqual((True, 4, True), (out["previews"], out["free"], out["known"]))

    def test_timers_are_read_once_per_ttl(self):
        _, jt = self._call({"Items": []})
        with mock.patch.object(L, "get_setting", lambda _db, k, d="": "x"), \
                mock.patch.object(L, "_jellyfin_timers", jt), mock.patch.object(L, "overall_stream_limit", return_value=4):
            asyncio.run(L.live_preview(previewing=False, user=object(), db=mock.Mock()))
        self.assertEqual(1, jt.call_count)

    def test_jellyfin_unreadable_means_no_preview(self):
        out, _ = self._call(error=RuntimeError("down"))
        self.assertEqual((False, False), (out["previews"], out["known"]))

    def test_a_reserved_channel_counts_as_taken(self):
        L._reserved_channels[5] = {"until": 1e12, "stream_key": "105"}
        out, _ = self._call({"Items": []}, limit=2)
        self.assertEqual((False, 1, 1), (out["previews"], out["recordings_soon"], out["free"]))
