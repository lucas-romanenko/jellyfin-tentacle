"""#288: a channel added just before a restart stayed half-added.

Adding a channel starts its first index in a background thread whose queue
lives in memory. A restart during that index left the rows found so far with
no files, no playlist and a card saying "Fetching its newest N videos…", until
the next scheduled check (a random point up to an hour later), or for good with
background checks off. Nothing at start-up looked for channels that were never
fully listed.

Run from tentacle/:  python -m unittest discover -s tests -p test_youtube_resume_first_index.py
"""
import unittest
from datetime import datetime
from unittest import mock

from models.database import set_setting
from test_youtube_traffic import _Db


class UnfinishedFirstIndexIsResumed(_Db):
    def _resume(self):
        from routers import youtube as yt_router
        with mock.patch.object(yt_router.client, "available", return_value=True), \
             mock.patch.object(yt_router, "_start_refresh") as start:
            queued = yt_router.resume_unfinished_channels()
        return queued, start

    def test_a_channel_never_listed_is_queued_at_start_up(self):
        set_setting(self.db, "youtube_enabled", "true")
        half = self.channel(title="Half", slug="half", channel_id="UC" + "h" * 22)
        self.channel(title="Done", slug="done", channel_id="UC" + "d" * 22,
                     last_full_check=datetime(2026, 9, 28))
        queued, start = self._resume()
        self.assertEqual([half.id], queued)
        start.assert_called_once_with(channel_ids=[half.id])

    def test_it_also_runs_with_background_checks_off(self):
        set_setting(self.db, "youtube_enabled", "true")
        set_setting(self.db, "youtube_background_checks", "false")
        half = self.channel()
        queued, _ = self._resume()
        self.assertEqual([half.id], queued)

    def test_nothing_is_queued_on_a_normal_restart(self):
        set_setting(self.db, "youtube_enabled", "true")
        self.channel(last_full_check=datetime(2026, 9, 28))
        self.channel(title="Off", slug="off", channel_id="UC" + "o" * 22, enabled=False)
        queued, start = self._resume()
        self.assertEqual([], queued)
        start.assert_not_called()

    def test_nothing_is_queued_while_the_source_is_off(self):
        self.channel()
        queued, start = self._resume()
        self.assertEqual([], queued)
        start.assert_not_called()

    def test_start_up_schedules_the_resume(self):
        import main
        with mock.patch.object(main, "schedule_once") as once, \
             mock.patch.object(main, "scheduler"), \
             mock.patch.object(main, "SessionLocal", self.Session):
            main.setup_scheduler(self.db)
        jobs = [c.args[2] for c in once.call_args_list]
        self.assertIn("youtube_resume_first_index", jobs)


if __name__ == "__main__":
    unittest.main()
