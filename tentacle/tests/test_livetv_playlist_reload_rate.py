"""A live HLS playlist is reloaded about once per segment, not twice.

The worker in routers/livetv.py slept `target_duration / 2` before every
playlist reload, whatever the last reload had brought. A live playlist gains one
segment per target duration, so every second reload was unchanged by
construction: the stream made twice the playlist requests it needed, and each of
them is a momentary connection in the provider's accounting -- the accounting
that answers 509 and splits recordings.

RFC 8216 6.3.4 is the rule to follow: wait a target duration after a reload that
changed the playlist, half of one after a reload that did not. And the wait runs
from when the playlist was read, so time spent downloading segments is not added
on top of it (that would let segments roll out of a short window).

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import asyncio
import unittest
from unittest.mock import patch

import httpx

PLAYLIST_CT = {"content-type": "application/vnd.apple.mpegurl"}
ROOT = "http://provider.test/live/u/p/"
BASE = ROOT + "1.m3u8"
TARGET = 6


def _playlist(*chunks, end=False):
    lines = ["#EXTM3U", "#EXT-X-VERSION:3", f"#EXT-X-TARGETDURATION:{TARGET}"]
    for c in chunks:
        lines += [f"#EXTINF:{TARGET}.0,", c]
    if end:
        lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"


def _resp(status, url, content=b"", headers=None):
    return httpx.Response(status, headers=headers or {}, content=content,
                          request=httpx.Request("GET", url))


def _pl(*chunks, end=False):
    return _resp(200, BASE, _playlist(*chunks, end=end).encode(), PLAYLIST_CT)


class FakeClient:
    def __init__(self, script, events, chunk_delay=0.0, **kwargs):
        self._script = script
        self._events = events
        self._chunk_delay = chunk_delay

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def aclose(self):
        return None

    def build_request(self, method, url, headers=None):
        return httpx.Request(method, url, headers=headers)

    async def send(self, request, stream=False):
        url = str(request.url)
        if url.endswith(".ts") and self._chunk_delay:
            await _REAL_SLEEP(self._chunk_delay)
        queue = self._script[url]
        resp = queue[0] if len(queue) == 1 else queue.pop(0)
        self._events.append(("GET", url, resp))
        return resp


_REAL_SLEEP = asyncio.sleep


async def _drive(script, chunk_delay=0.0):
    """Run the proxy; return the ordered events: ("GET", url, resp) / ("SLEEP", s)."""
    import routers.livetv as livetv

    events = []

    async def fast_sleep(delay):
        events.append(("SLEEP", delay))
        await _REAL_SLEEP(0)

    with patch("httpx.AsyncClient", lambda **kw: FakeClient(script, events, chunk_delay, **kw)), \
            patch("routers.livetv.is_safe_url", lambda *a, **k: True), \
            patch("asyncio.sleep", fast_sleep):
        response = await livetv._stream_proxy_inner(
            channel_id=1, user_agent="TestAgent/1.0", stream_url=BASE,
            _release_sem=lambda: None)

        async def collect():
            out = b""
            async for piece in response.body_iterator:
                out += piece
            return out

        body = await asyncio.wait_for(collect(), timeout=30)
    return body, events


def _chunks(n):
    return {f"{ROOT}c{i}.ts": [_resp(200, f"{ROOT}c{i}.ts", f"CHUNK{i};".encode())]
            for i in range(1, n + 1)}


def _waits_before_reloads(events):
    """For each playlist reload made by the running worker, the total time slept
    since the previous playlist read, and whether that previous read was new."""
    out = []
    slept = 0.0
    last_text = None
    last_was_new = None
    seen_first_chunk = False
    for ev in events:
        if ev[0] == "SLEEP":
            slept += ev[1]
            continue
        _, url, resp = ev
        if url.endswith(".ts"):
            seen_first_chunk = True
            continue
        if seen_first_chunk:
            out.append((slept, last_was_new))
        slept = 0.0
        # Reads made while opening the stream all count as the first, new one.
        last_was_new = (not seen_first_chunk) or resp.text != last_text
        last_text = resp.text
    return out


class TestPlaylistReloadRate(unittest.IsolatedAsyncioTestCase):
    def _steady_script(self, n=6):
        """A healthy live channel: every reload carries exactly one new segment."""
        names = [f"c{i}.ts" for i in range(1, n + 1)]
        opens = [_pl(*names[:2]), _pl(*names[:2])]
        reloads = [_pl(*names[:k]) for k in range(3, n + 1)] + [_pl(*names, end=True)]
        script = {BASE: opens + reloads}
        script.update(_chunks(n))
        return script

    async def test_a_playlist_that_brought_a_segment_is_not_reloaded_for_a_whole_segment(self):
        body, events = await _drive(self._steady_script())
        self.assertIn(b"CHUNK6;", body)
        waits = [w for w, was_new in _waits_before_reloads(events) if was_new]
        self.assertGreaterEqual(len(waits), 3)
        early = [w for w in waits if w < TARGET * 0.9]
        self.assertEqual(early, [],
                         f"reloaded a live playlist {len(early)}x sooner than one "
                         f"target duration ({TARGET}s) after a reload that had just "
                         f"delivered a new segment: waits={waits}. That is two "
                         f"provider requests per segment instead of one.")

    async def test_steady_state_costs_about_one_reload_per_segment(self):
        """Counted the way a provider counts: simulated seconds of stream per
        playlist request."""
        _, events = await _drive(self._steady_script(8))
        waits = _waits_before_reloads(events)
        per_reload = sum(w for w, _ in waits) / len(waits)
        self.assertGreaterEqual(per_reload, TARGET * 0.9,
                                f"averaged one reload per {per_reload:.1f}s on a "
                                f"{TARGET}s-segment stream")

    async def test_an_unchanged_playlist_is_asked_for_again_sooner(self):
        """The provider was late publishing: waiting a full target duration again
        is how segments roll out of a short window unseen."""
        names = ["c1.ts", "c2.ts", "c3.ts"]
        script = {BASE: [_pl(*names[:2]), _pl(*names[:2]),
                         _pl(*names[:2]),            # reload: nothing new
                         _pl(*names),                # retry: c3 arrived
                         _pl(*names, end=True)]}
        script.update(_chunks(3))
        body, events = await _drive(script)
        self.assertIn(b"CHUNK3;", body)
        stale = [w for w, was_new in _waits_before_reloads(events) if was_new is False]
        self.assertTrue(stale, "script never produced an unchanged reload")
        for w in stale:
            self.assertLessEqual(w, TARGET / 2 + 0.01,
                                 f"waited {w}s after an unchanged playlist")
            self.assertGreater(w, 0, "an unchanged playlist must not be re-polled in a tight loop")

    async def test_time_spent_downloading_is_not_added_to_the_wait(self):
        """The reload is due a target duration after the playlist was READ. A slow
        segment download eats into the wait rather than postponing the reload."""
        delay = 0.3
        body, events = await _drive(self._steady_script(4), chunk_delay=delay)
        self.assertIn(b"CHUNK4;", body)
        waits = [w for w, was_new in _waits_before_reloads(events) if was_new]
        self.assertTrue(waits)
        for w in waits:
            self.assertLess(w, TARGET - delay * 0.5,
                            f"slept {w}s after a pass that itself took {delay}s+: "
                            f"download time was added on top of the reload wait")


if __name__ == "__main__":
    unittest.main()
