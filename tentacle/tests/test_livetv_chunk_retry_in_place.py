"""A chunk refused with a transient 509 must be retried before it rolls away.

#86 stopped marking a failed chunk as seen, so it is asked for again -- but only
after the backoff sleep *plus* the regular `target_duration / 2` wait *plus* a
successful playlist refresh (itself subject to the same 509s). A live playlist
is a short sliding window (three segments is typical), so by the time that
round-trip completes the chunk has often rolled out of the playlist and is gone
for good: the stream survives, the recording has a hole.

Phase-3 QA measured it: 6 streams, provider uncapped but answering 25% of
requests with 509. Every stream ran full length on the #86 retry logic, and 3 of
4 recordings were each missing one segment.

The chunk URL is still valid while we wait, so retry it in place a few times
first, and only fall back to re-reading the playlist if that does not work.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import unittest

from test_livetv_hls_transient_errors import (
    BASE, PLAYLIST_CT, _drive, _playlist, _resp,
)

C = "http://provider.test/live/u/p/c%d.ts"


def _chunk(n, *statuses):
    url = C % n
    return [_resp(s, url, b"CHUNK%d" % n if s == 200 else b"") for s in statuses]


class TestChunkRetriedInPlace(unittest.IsolatedAsyncioTestCase):
    def _script(self, c2_statuses):
        first = _playlist("c1.ts", "c2.ts")
        # By the next refresh the window has moved on: c2 is no longer listed.
        rolled = _playlist("c3.ts", end=True)
        return {
            BASE: [_resp(200, BASE, first.encode(), PLAYLIST_CT),
                   _resp(200, BASE, first.encode(), PLAYLIST_CT),
                   _resp(200, BASE, rolled.encode(), PLAYLIST_CT)],
            C % 1: _chunk(1, 200),
            C % 2: _chunk(2, *c2_statuses),
            C % 3: _chunk(3, 200),
        }

    async def test_a_refused_chunk_is_not_lost_when_the_window_moves_on(self):
        body, log, _ = await _drive(self._script([509, 200]))
        self.assertIn(b"CHUNK2", body,
                      "the chunk was only retried after a playlist refresh, by "
                      "which time it had rolled out of the window: a hole in "
                      "the recording")
        self.assertLess(body.index(b"CHUNK2"), body.index(b"CHUNK3"), "out of order")

    async def test_in_place_retries_wait_and_grow(self):
        _, _, slept = await _drive(self._script([509, 509, 509, 200]))
        self.assertGreaterEqual(len(slept), 3)
        self.assertGreater(slept[2], slept[0])

    async def test_in_place_retries_are_bounded(self):
        """A chunk that stays refused must not pin the worker: after a few tries
        it goes back to the playlist, as before."""
        body, log, _ = await _drive(self._script([509] * 50 + [200]))
        self.assertIn(b"CHUNK3", body)
        self.assertLess(log.count(C % 2), 10)

    async def test_a_fatal_chunk_status_is_not_retried_in_place(self):
        body, log, slept = await _drive(self._script([404, 200]))
        self.assertEqual(1, log.count(C % 2))


if __name__ == "__main__":
    unittest.main()
