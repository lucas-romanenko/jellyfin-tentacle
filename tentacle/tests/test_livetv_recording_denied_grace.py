"""A recording rides out a short 401/403 from the provider (#646).

Run from the tentacle/ directory:  python -m unittest discover -s tests

An IP block, Cloudflare in front of a panel, or a panel's flood protection
answers 403 to every request (channel URL, token URLs) for a minute or two,
then everything works again. A recording ended within seconds of the first
403: the raw re-dial treated 401/403 as final, and the HLS worker spent its
_MAX_RERESOLVE re-resolves in ~3 s. Jellyfin does not start a recording
again when the tuner stream ends early, so the rest of the programme was
lost.

Now, on a RECORDING only, a 401/403 is waited out on the refusal cap for
_DENIED_GRACE of unbroken refusal, then the stream ends as before (a login
that is really refused costs a bounded number of tries, in the spirit of
#149). A viewer keeps today's quick end.
"""
import unittest
from unittest.mock import patch

import routers.livetv as livetv
from test_livetv_open_single_fetch import PANEL, TOKENIZED, _redirect, _resp
from test_livetv_raw_reconnect import _dropped, _live
from test_livetv_reconnect_budget import _play
from test_livetv_token_expiry import CHANNEL, Panel, _r, _stream

A = b"G" + b"a" * 187          # one TS packet
B = b"G" + b"b" * 187
CAP = livetv._REFUSAL_BACKOFF_CAP * 1.2 + 0.01   # the refusal cap, with jitter


class RawRecording(unittest.IsolatedAsyncioTestCase):
    async def test_a_recording_rides_out_a_minute_of_403_or_401(self):
        for status in (403, 401):
            n = 8                                 # ~75 s of backoff once it reaches the cap
            script = {PANEL: [_redirect()] + [_resp(status, PANEL)] * n + [_redirect(), _resp(404, PANEL)],
                      TOKENIZED: [_live([A], then=_dropped()), _live([B])]}
            body, log, slept = await _play(script, failure_budget=120, is_recording=lambda: True)
            self.assertEqual(A + B, body, f"{status}: the recording ended on the first refusal")
            self.assertGreater(sum(slept), 60, f"{status}: the block was not waited out")
            self.assertTrue(all(s <= CAP for s in slept), f"{status}: backoff above the refusal cap")

    async def test_a_recording_refused_for_good_still_ends_after_a_bounded_wait(self):
        script = {PANEL: [_redirect(), _resp(403, PANEL)], TOKENIZED: [_live([A], then=_dropped())]}
        body, log, slept = await _play(script, failure_budget=0, is_recording=lambda: True)
        self.assertEqual(A, body)
        # at least the drill's longest block (120 s), never a whole programme
        self.assertGreaterEqual(sum(slept), 120, "a recording gave up on 403 before two minutes")
        self.assertLessEqual(sum(slept), 300 + CAP)
        self.assertLessEqual(log.count(PANEL) - 1, 30, "too many tries against a refused login")


class RawViewer(unittest.IsolatedAsyncioTestCase):
    async def test_a_viewer_still_stops_at_once_on_403(self):
        script = {PANEL: [_redirect(), _resp(403, PANEL), _redirect()],
                  TOKENIZED: [_live([A], then=_dropped()), _live([B])]}
        body, log, slept = await _play(script, failure_budget=120, is_recording=lambda: False)
        self.assertEqual(A, body)
        self.assertEqual(1, len(slept))


def _ip_block(panel, start_seq, n, status=403):
    """Once the live window has reached start_seq, the next n requests of any
    kind (channel URL, playlist, segments) answer `status`."""
    real = panel.route
    left = {"n": n}

    def route(url):
        if panel.seq >= start_seq and left["n"] > 0:
            left["n"] -= 1
            panel.log.append(url)
            return _r(status, url)
        return real(url)
    panel.route = route
    return panel


def _consecutive(got):
    return got == list(range(got[0], got[0] + len(got))) if got else True


class HlsRecording(unittest.IsolatedAsyncioTestCase):
    async def test_a_recording_rides_out_an_ip_block(self):
        for status in (403, 401):
            panel = _ip_block(Panel(ttl=50), start_seq=4, n=30, status=status)
            got, ended = await _stream(panel, segments=20, recording=True)
            self.assertFalse(ended, f"{status} for a while ended the recording")
            self.assertTrue(_consecutive(got), f"{status}: hole or repeat {got}")

    async def test_a_recording_refused_for_good_still_ends_after_the_grace(self):
        panel = _ip_block(Panel(ttl=50), start_seq=4, n=10 ** 9)
        with patch.object(livetv, "_DENIED_GRACE", 0.3, create=True):
            got, ended = await _stream(panel, segments=50, recording=True)
        self.assertTrue(ended, "a login refused for good must still end the recording")
        self.assertGreater(panel.log.count(CHANNEL), 2 + livetv._MAX_RERESOLVE,
                           "the recording ended without waiting the refusal out")
        self.assertTrue(livetv._recent_streams[-1]["ended_on_error"])


class HlsViewer(unittest.IsolatedAsyncioTestCase):
    async def test_a_viewer_still_ends_after_three_re_resolves(self):
        panel = _ip_block(Panel(ttl=50), start_seq=4, n=30)
        got, ended = await _stream(panel, segments=20, recording=False)
        self.assertTrue(ended)
        self.assertLessEqual(panel.log.count(CHANNEL), 2 + livetv._MAX_RERESOLVE)


if __name__ == "__main__":
    unittest.main()
