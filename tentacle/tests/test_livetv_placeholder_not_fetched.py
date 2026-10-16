"""A redirect to a provider placeholder is refused BEFORE it is fetched (#624).

Run from the tentacle/ directory:  python -m unittest discover -s tests

#140 recognises a placeholder (black.ts, channel_unavailable.ts, ...) by name
and never fetches it -- when a playlist names it. A redirect to one was only
checked after _send_checked() had already sent the GET to it: every raw re-dial
of a channel the provider had parked fetched black.ts and then refused it, one
more request to the provider each time while a recording waited for the
channel to come back. The playlist refresh did not check the final URL at all
and read black.ts as a playlist.

_send_checked() checks each hop's name before it fetches it and raises
_ProviderPlaceholder there. Test scripts from therobmilne's report (#624).
"""
import unittest
from unittest.mock import patch

import routers.livetv as livetv
from test_livetv_hls_transient_errors import BASE, PLAYLIST_CT, _playlist, _resp
from test_livetv_open_single_fetch import PANEL, TOKENIZED, FakeClient, _redirect
from test_livetv_placeholder_refused import (BLACK, MASTER, MASTER_URL, VARIANT,
                                             RefusedAtOpen, RunningStreams)
from test_livetv_raw_reconnect import _dropped, _live, _play


class SendChecked(unittest.IsolatedAsyncioTestCase):
    async def test_a_redirect_to_a_placeholder_raises_before_the_get(self):
        log = []
        client = FakeClient({
            PANEL: [_resp(302, PANEL, headers={"location": BLACK})],
            BLACK: [_resp(200, BLACK, b"G" * 188, {"content-type": "video/mp2t"})],
        }, log, [])
        with patch("routers.livetv.is_safe_url", lambda *a, **k: True):
            with self.assertRaises(livetv._ProviderPlaceholder) as cm:
                await livetv._send_checked(client, PANEL, {})
        self.assertEqual("black.ts", cm.exception.segment)
        self.assertEqual([PANEL], log)


class PlaceholderNeverFetched(RefusedAtOpen, RunningStreams):
    async def test_open_redirected_to_a_placeholder_does_not_fetch_it(self):
        log = await self._refused({
            PANEL: [_resp(302, PANEL, headers={"location": BLACK})],
            BLACK: [_resp(200, BLACK, b"G" * 188, {"content-type": "video/mp2t"})],
        }, PANEL)
        self.assertNotIn(BLACK, log, "the placeholder was fetched before it was refused")
        self.assertEqual(1, livetv._placeholders[220]["count"])

    async def test_open_variant_redirected_to_a_placeholder_does_not_fetch_it(self):
        log = await self._refused({
            MASTER_URL: [_resp(200, MASTER_URL, MASTER.encode(), PLAYLIST_CT)],
            VARIANT: [_resp(302, VARIANT, headers={"location": BLACK})],
            BLACK: [_resp(200, BLACK, b"G" * 188)],
        }, MASTER_URL)
        self.assertNotIn(BLACK, log, "the placeholder was fetched before it was refused")

    async def test_raw_redial_redirected_to_a_placeholder_does_not_fetch_it(self):
        script = {
            PANEL: [_redirect(), _resp(302, PANEL, headers={"location": BLACK}), _redirect(),
                    _resp(404, PANEL)],
            TOKENIZED: [_live([b"AAAA"], then=_dropped()), _live([b"BBBB"])],
            BLACK: [_live([b"BLACK"], url=BLACK)],
        }
        body, log, *_ = await _play(script)
        self.assertEqual(b"AAAABBBB", body)
        self.assertNotIn(BLACK, log, "the placeholder was fetched before it was refused")
        self.assertEqual(1, livetv._placeholders[1]["count"])

    async def test_segment_redirected_to_a_placeholder_does_not_fetch_it(self):
        seg = "http://provider.test/live/u/p/"
        script = {
            BASE: [_resp(200, BASE, _playlist("c1.ts", "c2.ts").encode(), PLAYLIST_CT),
                   _resp(200, BASE, _playlist("c1.ts", "c2.ts", "c3.ts", end=True).encode(), PLAYLIST_CT)],
            seg + "c1.ts": [_resp(200, seg + "c1.ts", b"CHUNK1")],
            seg + "c2.ts": [_resp(302, seg + "c2.ts", headers={"location": BLACK}),
                            _resp(200, seg + "c2.ts", b"CHUNK2")],
            seg + "c3.ts": [_resp(200, seg + "c3.ts", b"CHUNK3")],
            BLACK: [_resp(200, BLACK, b"BLACK")],
        }
        body, log = await self._record(script)
        self.assertEqual(b"CHUNK1CHUNK2CHUNK3", body)
        self.assertNotIn(BLACK, log, "the placeholder was fetched before it was refused")

    async def test_playlist_refresh_redirected_to_a_placeholder_does_not_fetch_it(self):
        """The refresh never looked at the final URL: black.ts was read as the
        playlist. Waited out like any placeholder, and the stream carries on."""
        seg = "http://provider.test/live/u/p/"
        script = {
            BASE: [_resp(200, BASE, _playlist("c1.ts").encode(), PLAYLIST_CT),
                   _resp(302, BASE, headers={"location": BLACK}),
                   _resp(200, BASE, _playlist("c1.ts", "c2.ts", end=True).encode(), PLAYLIST_CT)],
            seg + "c1.ts": [_resp(200, seg + "c1.ts", b"CHUNK1")],
            seg + "c2.ts": [_resp(200, seg + "c2.ts", b"CHUNK2")],
            BLACK: [_resp(200, BLACK, b"BLACK")],
        }
        body, log = await self._record(script)
        self.assertEqual(b"CHUNK1CHUNK2", body)
        self.assertNotIn(BLACK, log, "the placeholder was fetched before it was refused")
        self.assertEqual(1, livetv._placeholders[1]["count"])


def load_tests(loader, tests, pattern):
    # The imported RefusedAtOpen and RunningStreams run from their own module;
    # here only the tests above.
    suite = unittest.TestSuite()
    suite.addTests(loader.loadTestsFromTestCase(SendChecked))
    for name in loader.getTestCaseNames(PlaceholderNeverFetched):
        if "does_not_fetch_it" in name:
            suite.addTest(PlaceholderNeverFetched(name))
    return suite


if __name__ == "__main__":
    unittest.main()
