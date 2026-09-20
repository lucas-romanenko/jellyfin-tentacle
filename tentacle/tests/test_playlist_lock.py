"""#32: every playlist mutation runs under the refresh lock.

refresh_smartlist_playlists() took _playlist_refresh_lock, but the toggle /
sync-one fast paths, the webhook add + remove paths and the orphan sweep
mutated the same Jellyfin playlists without it, so a webhook add could land in
the middle of a refresh's clear + re-add.

Run from tentacle/:  python -m unittest discover -s tests
"""
import threading
import unittest

import services.smartlists as sl


class RecordingLock:
    """Stands in for the module lock and records acquisitions."""

    def __init__(self):
        self.acquired = 0
        self._real = threading.RLock()

    def __enter__(self):
        self.acquired += 1
        return self._real.__enter__()

    def __exit__(self, *exc):
        return self._real.__exit__(*exc)


class TestLockIsReentrant(unittest.TestCase):
    def test_lock_is_an_rlock(self):
        # refresh_smartlist_playlists() holds the lock while calling
        # _process_single_playlist(), which now takes it again.
        lock = sl._playlist_refresh_lock
        self.assertTrue(hasattr(lock, "acquire"))
        with lock:
            acquired_again = lock.acquire(blocking=False)
            if acquired_again:
                lock.release()
        self.assertTrue(acquired_again, "_playlist_refresh_lock must be re-entrant")


class TestMutatorsTakeTheLock(unittest.TestCase):
    """Each public mutator must go through the lock before touching Jellyfin."""

    MUTATORS = [
        "_process_single_playlist",
        "cleanup_orphaned_playlists",
        "remove_item_from_playlists",
        "add_item_to_matching_playlists",
    ]

    def test_each_mutator_has_a_locked_inner(self):
        for name in self.MUTATORS:
            with self.subTest(mutator=name):
                self.assertTrue(hasattr(sl, name), f"{name} is missing")
                self.assertTrue(
                    hasattr(sl, f"_{name.lstrip('_')}_locked"),
                    f"{name} must delegate to a _locked inner under the lock",
                )

    def test_wrapper_acquires_the_lock(self):
        lock = RecordingLock()
        original_lock = sl._playlist_refresh_lock
        original_inner = sl._remove_item_from_playlists_locked
        sl._playlist_refresh_lock = lock
        sl._remove_item_from_playlists_locked = lambda *a, **k: {"ok": True}
        try:
            result = sl.remove_item_from_playlists(None, "item-1", 1)
        finally:
            sl._playlist_refresh_lock = original_lock
            sl._remove_item_from_playlists_locked = original_inner
        self.assertEqual(result, {"ok": True})
        self.assertEqual(lock.acquired, 1)


if __name__ == "__main__":
    unittest.main()
