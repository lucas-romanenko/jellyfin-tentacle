"""tuliprox's custom videos are placeholders too, whatever their file name (#140).

Run from the tentacle/ directory:  python -m unittest discover -s tests

A placeholder is recognised by its file name (black.ts, channel_unavailable.ts,
...). tuliprox 3.3 serves its custom videos -- channel unavailable, connections
exhausted, low priority preempted, account expired, provisioning -- under its
/cvs/ route, and in an HLS playlist as twelve numbered segments of the clip:

    /cvs/hls/<access lease>/<asset fingerprint>/0.ts ... 11.ts   + #EXT-X-ENDLIST

and provisioning as /cvs/hls/<user>/<pass>/provisioning_<n>.ts. None of those
names is in the list, so the clip was proxied as the channel: a recording
opened on it was saved as the clip and filed as complete (the #140 failure),
and a running stream piped the clip and ended. Two of tuliprox's reason names
(low_priority_preempted, panel_api_provisioning) were missing from the list as
well. Recognised now by tuliprox's route, /cvs/{hls|ts}/<a>/<b>/<file>.ts.
"""
import unittest

import routers.livetv as livetv
from test_livetv_hls_transient_errors import BASE, PLAYLIST_CT, _drive, _playlist, _resp

CVS = "http://provider.test/cvs/hls/0f3c2a9e7b1d4c55/9a8b7c6d5e4f3a2b"
CLIP = [f"{CVS}/{n}.ts" for n in range(12)]     # the shape tuliprox writes


class Names(unittest.TestCase):
    def test_numbered_custom_video_segments_are_placeholders(self):
        for url in CLIP + ["https://example.test/iptv/cvs/hls/viewer/secret/provisioning_000.ts",
                           "http://provider.test/cvs/ts/viewer/secret/3.ts?x=1"]:
            self.assertEqual(url.split("?")[0].rsplit("/", 1)[-1], livetv._placeholder_name(url), url)

    def test_every_tuliprox_reason_name_is_a_placeholder(self):
        for name in ("low_priority_preempted", "panel_api_provisioning"):
            url = f"http://provider.test/cvs/viewer/secret/{name}.ts"
            self.assertEqual(f"{name}.ts", livetv._placeholder_name(url), url)

    def test_real_segments_still_are_not(self):
        for url in ("http://provider.test/hls/0f3c2a9e/abc/0.ts",
                    "http://provider.test/live/u/p/220/0.ts",
                    "http://provider.test/hlsr/cvs/secret/220/7/1.ts",    # an account named "cvs"
                    "http://provider.test/cvs/hls/0f3c/9a8b/index.m3u8",
                    "http://provider.test/cvs/hls/0f3c/0.ts"):
            self.assertIsNone(livetv._placeholder_name(url), url)


class Streams(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        livetv._placeholders.clear()
        livetv._placeholder_activity_at.clear()
        real = livetv._record_activity_for_placeholder
        livetv._record_activity_for_placeholder = lambda cid, seg: None
        self.addCleanup(setattr, livetv, "_record_activity_for_placeholder", real)

    async def test_a_channel_answering_with_the_clip_is_refused_at_open(self):
        """What a recording opening on it now gets: a 503 before any byte, so
        Jellyfin fails the timer and retries it, instead of saving the clip."""
        from unittest.mock import patch
        from fastapi import HTTPException
        from test_livetv_open_single_fetch import FakeClient

        script = {BASE: [_resp(200, BASE, _playlist(*CLIP, end=True).encode(), PLAYLIST_CT)]}
        script.update({u: [_resp(200, u, b"CLIP")] for u in CLIP})
        log = []
        with patch("routers.livetv.is_safe_url", lambda *a, **k: True), \
                patch("httpx.AsyncClient", lambda **kw: FakeClient(script, log, [], **kw)):
            with self.assertRaises(HTTPException) as cm:
                await livetv._stream_proxy_inner(channel_id=220, user_agent="t", stream_url=BASE,
                                                 _release_sem=lambda: None)
        self.assertEqual(503, cm.exception.status_code)
        self.assertIn("placeholder", cm.exception.detail)
        self.assertEqual([BASE], log, "the clip itself must never be fetched")
        self.assertEqual(1, livetv._placeholders[220]["count"])

    async def test_a_running_stream_does_not_pipe_the_clip(self):
        """tuliprox ends a channel that failed with the clip and #EXT-X-ENDLIST."""
        seg = "http://provider.test/live/u/p/"
        script = {
            BASE: [_resp(200, BASE, _playlist("c1.ts").encode(), PLAYLIST_CT),
                   _resp(200, BASE, _playlist("c1.ts", *CLIP, end=True).encode(), PLAYLIST_CT)],
            seg + "c1.ts": [_resp(200, seg + "c1.ts", b"CHUNK1")],
        }
        script.update({u: [_resp(200, u, b"CLIP")] for u in CLIP})
        body, log, _slept = await _drive(script)
        self.assertEqual(b"CHUNK1", body)
        self.assertFalse([u for u in log if "/cvs/" in u], "a clip segment was fetched")


if __name__ == "__main__":
    unittest.main()
