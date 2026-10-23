"""A deploy must not restart Tentacle or Jellyfin in the middle of a
recording, nor in the minutes before one (its pre-padding). Recordings are
not playback sessions, so "nothing playing" can't see them:
GET /api/live/recordings/busy reads Jellyfin's timers instead.

Also: a one-off moved to the lineup's channel keeps its priority (#765).

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import unittest
from datetime import datetime
from unittest import mock

from routers import recordings as R

NOW = datetime(2026, 10, 24, 23, 0, 0)


def timer(status, start, end, pre=0, post=0, name="NHL Hockey"):
    return {"Status": status, "StartDate": start, "EndDate": end, "Name": name,
            "PrePaddingSeconds": pre, "PostPaddingSeconds": post, "ChannelName": "Sportsnet"}


class Busy(unittest.TestCase):
    def test_nothing_booked_is_not_busy(self):
        out = R.busy_from_timers([], NOW)
        self.assertEqual(([], [], None), (out["recording"], out["starting_soon"], out["next_start"]))

    def test_a_recording_in_progress_is_busy(self):
        out = R.busy_from_timers([timer("InProgress", "2026-10-24T22:30:00.0000000Z",
                                        "2026-10-25T01:00:00.0000000Z")], NOW)
        self.assertEqual(1, len(out["recording"]))

    def test_a_timer_inside_its_pre_padding_is_recording(self):
        # starts 23:10 with 15 min pre-padding: Jellyfin opened it at 22:55
        out = R.busy_from_timers([timer("New", "2026-10-24T23:10:00Z", "2026-10-25T01:40:00Z", pre=900)], NOW)
        self.assertEqual(1, len(out["recording"]))

    def test_a_timer_starting_soon_counts_with_its_padding(self):
        # 23:30 minus 15 min = 23:15, within 20 minutes
        out = R.busy_from_timers([timer("New", "2026-10-24T23:30:00.000Z", "2026-10-25T02:00:00.000Z", pre=900)], NOW)
        self.assertEqual(([], 1), (out["recording"], len(out["starting_soon"])))
        # without the padding it would be 30 minutes away: not soon
        out = R.busy_from_timers([timer("New", "2026-10-24T23:30:00Z", "2026-10-25T02:00:00Z")], NOW)
        self.assertEqual([], out["starting_soon"])
        self.assertEqual("2026-10-24T23:30:00Z", out["next_start"])

    def test_a_stale_new_timer_after_its_end_is_not_busy(self):
        out = R.busy_from_timers([timer("New", "2026-10-24T19:00:00Z", "2026-10-24T21:00:00Z", post=3600)], NOW)
        self.assertEqual(([], [], None), (out["recording"], out["starting_soon"], out["next_start"]))

    def test_post_padding_keeps_it_busy(self):
        out = R.busy_from_timers([timer("InProgress", "2026-10-24T20:00:00Z", "2026-10-24T22:30:00Z", post=3600)],
                                 NOW)
        self.assertEqual(1, len(out["recording"]))

    def test_cancelled_and_completed_timers_dont_count(self):
        items = [timer(s, "2026-10-24T23:05:00Z", "2026-10-25T01:00:00Z") for s in ("Cancelled", "Completed")]
        self.assertEqual([], R.busy_from_timers(items, NOW)["starting_soon"])


class Endpoint(unittest.TestCase):
    def _db(self, url="http://jf.test", key="k"):
        db = mock.Mock()
        values = {"jellyfin_url": url, "jellyfin_api_key": key}
        return db, (lambda _db, k, d="": values.get(k, d))

    def test_jellyfin_down_is_not_known(self):
        db, gs = self._db()
        with mock.patch.object(R, "get_setting", gs), \
                mock.patch("routers.livetv._jellyfin_timers", side_effect=RuntimeError("down")):
            out = R.recordings_busy(soon_minutes=20, db=db)
        self.assertFalse(out["known"])

    def test_busy_when_a_timer_is_due(self):
        db, gs = self._db()
        items = {"Items": [timer("New", "2099-01-01T00:00:00Z", "2099-01-01T03:00:00Z")]}
        with mock.patch.object(R, "get_setting", gs), \
                mock.patch("routers.livetv._jellyfin_timers", return_value=items):
            out = R.recordings_busy(soon_minutes=20, db=db)
        self.assertEqual((True, False), (out["known"], out["busy"]))
        self.assertEqual("2099-01-01T00:00:00Z", out["next_start"])


class MoveKeepsPriority(unittest.TestCase):
    def test_a_moved_one_off_keeps_padding_and_priority(self):
        from services import livetv_tuners as T
        posted = []

        class Jf:
            def get(self, path, **p):
                if path == "/LiveTv/SeriesTimers":
                    return {"Items": []}
                if path == "/LiveTv/Programs":
                    return {"Items": [{"Id": "prog"}]}
                if path == "/LiveTv/Timers/Defaults":
                    return {"ProgramId": "prog", "ChannelId": "new", "Name": "NHL Hockey",
                            "PrePaddingSeconds": 0, "Priority": 0}
                raise AssertionError(path)

            def post(self, path, body=None, **p):
                posted.append((path, body))

            def delete(self, path, **p):
                pass

        one = {"Id": "t1", "ChannelId": "old", "StartDate": "2026-10-24T23:00:00Z", "Name": "NHL Hockey",
               "PrePaddingSeconds": 900, "PostPaddingSeconds": 3600, "Priority": 7, "KeepUntil": "UntilDeleted"}
        with mock.patch.object(T, "_channel_id", lambda t: t.get("ChannelId")):
            moved = T.move_schedules(Jf(), {"old": "c1"}, {"new": "c1"}, one_offs=[one])
        self.assertEqual(1, moved)
        body = posted[0][1]
        self.assertEqual((900, 3600, 7, "UntilDeleted"),
                         (body["PrePaddingSeconds"], body["PostPaddingSeconds"], body["Priority"], body["KeepUntil"]))


if __name__ == "__main__":
    unittest.main()
