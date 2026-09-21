"""A request-path playlist mutation must not block for as long as a refresh runs.

The toggle and custom-playlist save go through _process_single_playlist, which
takes the refresh lock. A full resync or the nightly refresh holds that lock
for minutes, so a toggle issued meanwhile sat on a request thread until the
reverse proxy gave up — while the UI showed the optimistic state. It now waits
a bounded time and answers "busy".

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import threading
import unittest
from unittest import mock

import services.smartlists as smartlists


class TestFastPathGivesUpOnABusyLock(unittest.TestCase):
    def test_raises_busy_when_the_refresh_holds_the_lock(self):
        held = threading.Event()
        release = threading.Event()

        def hold():
            with smartlists._playlist_refresh_lock:
                held.set()
                release.wait(5)

        t = threading.Thread(target=hold, daemon=True)
        t.start()
        self.assertTrue(held.wait(2))
        try:
            with mock.patch.object(smartlists, "FAST_PATH_LOCK_TIMEOUT", 0.05):
                with self.assertRaises(smartlists.PlaylistsBusy):
                    smartlists._process_single_playlist(None, None, {}, "u", {})
        finally:
            release.set()
            t.join(2)

    def test_proceeds_once_the_lock_is_free(self):
        called = []
        with mock.patch.object(smartlists, "_process_single_playlist_locked",
                               lambda *a, **k: called.append(1) or "ok"):
            self.assertEqual(smartlists._process_single_playlist(None, None, {}, "u", {}), "ok")
        self.assertEqual(called, [1])
        # and the lock is released afterwards
        self.assertTrue(smartlists._playlist_refresh_lock.acquire(timeout=1))
        smartlists._playlist_refresh_lock.release()


if __name__ == "__main__":
    unittest.main()
