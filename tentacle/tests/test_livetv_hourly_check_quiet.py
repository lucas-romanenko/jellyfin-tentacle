"""The hourly Live TV check tells the clients only when it changed something.

After every Live TV setup run Tentacle calls POST /Tentacle/Refresh?scope=livetv
(services/livetv_tuners.tell_clients) and the plugin sends every connected
user a LibraryChanged carrying "tentacle-livetv:<version>". The TV app takes
that message as a home change without a home version: on Home it reloads every
row and the toolbar (HomeRowsFragment: push == null -> stockRows + force; the
navbars refetch the toolbar), in the player it marks Home dirty for later.

The setup run also runs every hour (main.py, job "livetv_setup") to catch
people added in Jellyfin. On a live install five hourly runs in a row changed
nothing (added/removed/tagged/policies/favourites/schedules all 0, no guide
refresh) and each one still sent the push: every TV that is on reloads its
whole Home once an hour for nothing.

A run someone asked for (schedule_sync: a person's Live TV access, a kids
profile, a place, the lineup) still tells the clients whatever it counted:
some of those changes are made in Jellyfin before the run (person_access posts
EnableLiveTvAccess itself), so the run's own counts can be all zero.

Run from the tentacle/ directory:  python -m unittest discover -s tests -p test_livetv_hourly_check_quiet.py
"""
import ast
import unittest
from pathlib import Path
from unittest import mock

MAIN = Path(__file__).resolve().parents[1] / "main.py"

NOTHING = {"added": 0, "removed": 0, "tagged": 0, "policies": 0, "favourites": 0,
           "place_favourites": 0, "schedules": 0, "refreshed": False, "backup": None,
           "guide_added": False}


def hourly_kwargs() -> dict:
    """The keyword arguments main.py gives the hourly "livetv_setup" job."""
    for node in ast.walk(ast.parse(MAIN.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "add_job":
            kw = {k.arg: k.value for k in node.keywords}
            job_id = kw.get("id")
            if isinstance(job_id, ast.Constant) and job_id.value == "livetv_setup":
                return ast.literal_eval(kw["kwargs"]) if "kwargs" in kw else {}
    raise AssertionError("main.py schedules no 'livetv_setup' job")


class HourlyCheckTellsOnlyAChange(unittest.TestCase):
    def _run(self, done, **kwargs):
        from services import livetv_tuners as T
        with mock.patch.object(T, "SessionLocal"), \
                mock.patch.object(T, "sync", return_value=dict(done)), \
                mock.patch.object(T, "log_activity"), \
                mock.patch.object(T, "set_setting"), \
                mock.patch.object(T, "tell_clients") as tell:
            T.run_sync(**kwargs)
        return tell

    def test_an_hourly_check_that_changed_nothing_tells_nobody(self):
        tell = self._run(NOTHING, **hourly_kwargs())
        tell.assert_not_called()

    def test_an_hourly_check_that_changed_something_tells_the_clients(self):
        for change in ({"added": 1}, {"removed": 1}, {"tagged": 3}, {"policies": 1},
                       {"favourites": 2}, {"place_favourites": 1}, {"schedules": 1},
                       {"refreshed": True}):
            with self.subTest(change=change):
                tell = self._run({**NOTHING, **change}, **hourly_kwargs())
                tell.assert_called_once()

    def test_a_run_someone_asked_for_still_tells_the_clients(self):
        # schedule_sync -> _run_pending -> run_sync(reason, refresh): e.g. "Live TV for Ann",
        # whose policy change was posted to Jellyfin before the run.
        tell = self._run(NOTHING, reason="Live TV for Ann")
        tell.assert_called_once()

    def test_a_skipped_run_tells_nobody(self):
        tell = self._run({**NOTHING, "skipped": "no Live TV channels"}, **hourly_kwargs())
        tell.assert_not_called()


if __name__ == "__main__":
    unittest.main()
