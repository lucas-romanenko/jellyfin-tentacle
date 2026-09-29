"""A raw MPEG-TS re-dial waits out a 407 or a non-standard 5xx (#298).

Run from the tentacle/ directory:  python -m unittest discover -s tests

Providers of this family answer 407 for an ended session, and panels behind
Cloudflare answer 513/520/522 for a few minutes now and then. The raw re-dial
treated every status outside _OPEN_RETRYABLE_STATUS as permanent, so one such
answer ended a running recording for good, although the channel worked again
a few minutes later. They are now waited out like a 509: within the viewer's
budget, and for as long as a recording is attached. 401/403/404 still stop.
"""
import unittest

from test_livetv_open_single_fetch import PANEL, TOKENIZED, _redirect, _resp
from test_livetv_raw_reconnect import _dropped, _live
from test_livetv_reconnect_budget import _play

A = b"G" + b"a" * 187          # one TS packet
B = b"G" + b"b" * 187


class RawRedialRefusals(unittest.IsolatedAsyncioTestCase):
    async def test_a_single_407_from_the_fresh_token_url_is_waited_out(self):
        script = {PANEL: [_redirect()] * 3 + [_resp(404, PANEL)],
                  TOKENIZED: [_live([A], then=_dropped()), _resp(407, TOKENIZED), _live([B])]}
        body, *_ = await _play(script, failure_budget=120)
        self.assertEqual(A + B, body)

    async def test_a_513_520_or_522_from_the_channel_url_is_waited_out(self):
        for status in (513, 520, 522):
            script = {PANEL: [_redirect(), _resp(status, PANEL), _redirect(), _resp(404, PANEL)],
                      TOKENIZED: [_live([A], then=_dropped()), _live([B])]}
            body, *_ = await _play(script, failure_budget=120)
            self.assertEqual(A + B, body, status)

    async def test_a_recording_outlasts_a_long_run_of_407(self):
        n = 40                                    # far past a 30 s viewer budget
        script = {PANEL: [_redirect()] * (n + 2) + [_resp(404, PANEL)],
                  TOKENIZED: [_live([A], then=_dropped())] + [_resp(407, TOKENIZED)] * n + [_live([B])]}
        body, log, slept = await _play(script, failure_budget=30, is_recording=lambda: True)
        self.assertEqual(A + B, body)
        self.assertTrue(all(s <= 15 * 1.2 for s in slept), "backoff above the refusal cap")


class StillBounded(unittest.IsolatedAsyncioTestCase):
    async def test_a_viewer_on_407_for_ever_gives_up_within_the_budget_without_hammering(self):
        script = {PANEL: [_redirect()], TOKENIZED: [_live([A], then=_dropped()), _resp(407, TOKENIZED)]}
        body, log, slept = await _play(script, failure_budget=60, is_recording=lambda: False)
        self.assertEqual(A, body)
        self.assertLessEqual(sum(slept), 60 + 18.1)
        self.assertLessEqual(log.count(PANEL) - 1, 20)

    async def test_401_still_stops_even_for_a_recording(self):
        script = {PANEL: [_redirect(), _resp(401, PANEL)], TOKENIZED: [_live([A], then=_dropped())]}
        body, log, slept = await _play(script, failure_budget=0, is_recording=lambda: True)
        self.assertEqual(A, body)
        self.assertEqual(1, len(slept))

    async def test_404_still_stops(self):
        script = {PANEL: [_redirect(), _resp(404, PANEL)], TOKENIZED: [_live([A], then=_dropped())]}
        body, log, slept = await _play(script, failure_budget=120, is_recording=lambda: True)
        self.assertEqual(A, body)
        self.assertEqual(1, len(slept))

    async def test_a_recording_on_407_for_ever_ends_when_the_recording_does(self):
        calls = {"n": 0}

        def rec():
            calls["n"] += 1
            return calls["n"] < 30
        script = {PANEL: [_redirect()], TOKENIZED: [_live([A], then=_dropped()), _resp(407, TOKENIZED)]}
        body, *_ = await _play(script, failure_budget=30, is_recording=rec)
        self.assertEqual(A, body)


if __name__ == "__main__":
    unittest.main()
