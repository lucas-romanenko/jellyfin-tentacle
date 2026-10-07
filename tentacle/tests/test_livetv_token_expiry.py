"""An expired tokenized HLS URL is re-resolved from the channel URL.

Seen in production (upstream 755ea67 backend, livetv_stream_format m3u8):
the provider's tokenized playlist URL stops working about an hour into a
stream and the next refresh answers 407 Proxy Authentication Required.
Tentacle treated that as fatal ("Playlist refresh failed fatally … 407")
and ended the response; a EuroLeague recording was cut at 63 min and
Jellyfin re-fired the timer 23 min later into a second file.

The channel URL (stream_url -> 302 -> tokenized URL) is what hands out a
fresh token. On 401/403/404/407/410 from a playlist refresh or a variant
hop -- and 401/403/407 from a segment -- the worker now resolves the
channel URL again, through the same checked sender (SSRF guard,
placeholder refusal), and carries on in the SAME response: counted as an
interruption, inside the viewer's budget, for as long as a recording stays
attached. Segments already sent are not sent again under their new token
URLs (media sequence). If the fresh resolve keeps answering the same way
_MAX_RERESOLVE times in a row with no segment in between, the account
really is refusing and the stream ends as before.

(The raw MPEG-TS path already re-dials from the channel URL: see
test_livetv_raw_reconnect.test_the_redial_starts_from_the_channel_url.)

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import asyncio
import unittest
from unittest.mock import patch

import httpx

import routers.livetv as livetv

CHANNEL = "http://p.test/live/u/p/7.m3u8"
PL = {"content-type": "application/vnd.apple.mpegurl"}
TS = {"content-type": "video/mp2t"}


def _r(status, url, content=b"", headers=None):
    return httpx.Response(status, headers=headers or {}, content=content,
                          request=httpx.Request("GET", url))


class Panel:
    """A provider whose tokens die after `ttl` playlist reads. The live
    window is 3 segments and moves one segment per playlist read; segment
    URLs carry the token, as real tokenized URLs do."""

    def __init__(self, ttl=3, master=False, channel_status=None, segment_expiry=False, renumber=False,
                 seq_shift=0):
        self.ttl, self.master = ttl, master
        self.channel_status = channel_status     # e.g. 407: the account itself refuses
        self.segment_expiry = segment_expiry     # the token dies on segments first
        self.renumber = renumber                 # each token restarts media sequence at 0
        self.offset = {}                         # token -> media sequence offset
        self.seq_shift = seq_shift               # each new token's numbering moves by this
        self.tokens = {}                         # token -> playlist reads left
        self.issued = 0
        self.seq = 0
        self.log = []

    def _token(self):
        self.issued += 1
        t = f"T{self.issued}"
        self.tokens[t] = self.ttl
        # a renumbering server starts a token's window at 0, with a backlog
        self.offset[t] = max(0, self.seq - 2) if (self.renumber and self.issued > 1) else 0
        if self.seq_shift:
            self.offset[t] = -self.seq_shift * (self.issued - 1)
        return t

    def route(self, url):
        self.log.append(url)
        if url == CHANNEL:
            if self.channel_status:
                return _r(self.channel_status, url)
            tok = self._token()
            target = f"http://edge.test/{tok}/master.m3u8" if self.master else f"http://edge.test/{tok}/index.m3u8"
            return _r(302, url, headers={"location": target})
        if url.endswith("/video/black.ts"):
            # upstream's #140 follows a redirect and refuses the clip by name
            return _r(200, url, headers={"content-type": "video/mp2t"})
        parts = url.split("/")
        tok = parts[3]
        left = self.tokens.get(tok, 0)
        if url.endswith("master.m3u8"):
            if left <= 0:
                return _r(407, url)
            return _r(200, url, b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nindex.m3u8\n", PL)
        if url.endswith("index.m3u8"):
            if left <= 0:
                return _r(407, url)
            self.tokens[tok] = left - 1
            self.seq += 1
            first = max(0, self.seq - 2)
            off = self.offset.get(tok, 0)
            first = max(first, off)
            lines = ["#EXTM3U", "#EXT-X-TARGETDURATION:2", f"#EXT-X-MEDIA-SEQUENCE:{first - off}"]
            for n in range(first, self.seq + 1):
                lines += ["#EXTINF:2.0,", f"{n}.ts"]
            return _r(200, url, ("\n".join(lines) + "\n").encode(), PL)
        if url.endswith(".ts"):
            if self.segment_expiry and left <= 1:
                return _r(407, url)
            n = int(parts[-1][:-3])
            return _r(200, url, b"G" + f"{n:04d}".encode() + b"\x00" * 183, TS)
        return _r(404, url)


class _Client:
    def __init__(self, panel):
        self.panel = panel

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def aclose(self):
        pass

    def build_request(self, method, url, headers=None):
        return httpx.Request(method, url, headers=headers)

    async def send(self, request, stream=False):
        await asyncio.sleep(0)     # a network round trip yields to the loop
        return self.panel.route(str(request.url))


SLEPT = []
EVENTS = []   # ("sleep", d) and whatever a test's route appends, in order


async def _stream(panel, segments=12, recording=False, budget=120.0):
    real_sleep = asyncio.sleep
    SLEPT.clear()
    EVENTS.clear()

    async def fast_sleep(d):
        SLEPT.append(d)
        EVENTS.append(("sleep", d))
        await real_sleep(0)

    livetv._recent_streams.clear()
    with patch("httpx.AsyncClient", lambda **kw: _Client(panel)), \
            patch("routers.livetv.is_safe_url", lambda *a, **k: True), \
            patch("asyncio.sleep", fast_sleep), \
            patch.object(livetv, "SessionLocal", side_effect=RuntimeError("no db")):
        resp = await livetv._stream_proxy_inner(
            channel_id=7, user_agent="UA", stream_url=CHANNEL, _release_sem=lambda: None,
            failure_budget=budget, is_recording=lambda: recording)
        got = []

        async def collect():
            async for piece in resp.body_iterator:
                for i in range(0, len(piece), 188):
                    got.append(int(piece[i + 1:i + 5]))
                if len(got) >= segments:
                    return
        ended = False
        try:
            await asyncio.wait_for(collect(), timeout=10)
            ended = len(got) < segments
        except asyncio.TimeoutError:
            pass
        await resp.body_iterator.aclose()
    return got, ended


class TokenExpiry(unittest.IsolatedAsyncioTestCase):
    async def test_an_expired_playlist_token_is_re_resolved_in_the_same_response(self):
        panel = Panel(ttl=3)
        got, ended = await _stream(panel, segments=12)
        self.assertFalse(ended, "the stream ended at the first expired token")
        self.assertEqual(list(range(len(got))), got, "segments repeated or missing across a re-resolve")
        self.assertGreaterEqual(panel.issued, 4, "no fresh token was fetched (3 expiries expected)")
        self.assertGreaterEqual(panel.log.count(CHANNEL), 4)

    async def test_it_counts_as_an_interruption(self):
        panel = Panel(ttl=3)
        await _stream(panel, segments=8)
        last = livetv._recent_streams[-1]
        self.assertGreaterEqual(last["reconnects"], 1)
        self.assertGreaterEqual(last["errors"], 1)

    async def test_a_master_playlist_is_followed_again_after_the_re_resolve(self):
        panel = Panel(ttl=3, master=True)
        got, ended = await _stream(panel, segments=10)
        self.assertFalse(ended)
        self.assertEqual(list(range(len(got))), got)
        self.assertGreaterEqual(panel.issued, 3)

    async def test_a_segment_refused_for_its_token_is_re_resolved(self):
        panel = Panel(ttl=4, segment_expiry=True)
        got, ended = await _stream(panel, segments=10)
        self.assertFalse(ended)
        self.assertEqual(list(range(len(got))), got)

    async def test_a_channel_that_keeps_refusing_still_ends(self):
        # 401 (was 407): a 407 from the channel url is what this provider family
        # answers for an ended session while a token is renewed, and a recording
        # now waits it out as the raw path does (9a1175e, #298). A refused login
        # still ends the stream. The 407 pair: test_livetv_hls_transient_refusals.
        panel = Panel(ttl=2)
        real_route = panel.route

        def route(url):
            if url == CHANNEL and panel.issued >= 1:
                panel.log.append(url)
                return _r(401, url)      # the account itself is refused now
            return real_route(url)
        panel.route = route
        got, ended = await _stream(panel, segments=50, recording=True)
        self.assertTrue(ended, "a really refused account must still end the stream")
        self.assertLessEqual(panel.log.count(CHANNEL), 1 + livetv._MAX_RERESOLVE)

    async def test_the_re_resolve_goes_through_the_placeholder_refusal(self):
        panel = Panel(ttl=2)
        real_route = panel.route

        def route(url):
            if url == CHANNEL and panel.issued >= 1:
                panel.log.append(url)
                return _r(302, url, headers={"location": "http://p.test/video/black.ts"})
            return real_route(url)
        panel.route = route
        got, _ = await _stream(panel, segments=50, budget=0.5)
        # (upstream's #140 follows the redirect and refuses the clip by name; it is never sent)



class ReResolveReview(unittest.IsolatedAsyncioTestCase):
    """Review of 03b2f2a (token_harness scenarios)."""

    def _flaky_channel(self, panel, bad, answer):
        real = panel.route
        state = {"n": 0}

        def route(url):
            if url == CHANNEL and panel.issued >= 1 and state["n"] < bad:
                state["n"] += 1
                panel.log.append(url)
                return answer(url)
            return real(url)
        panel.route = route

    async def test_resolve509_a_busy_provider_does_not_end_a_recording(self):
        panel = Panel(ttl=3)
        self._flaky_channel(panel, 5, lambda u: _r(509, u))
        got, ended = await _stream(panel, segments=12, recording=True)
        self.assertFalse(ended, "509s while re-resolving counted toward the limit")
        self.assertGreaterEqual(panel.issued, 2)

    async def test_resolve_black_a_placeholder_does_not_end_a_recording(self):
        panel = Panel(ttl=3)
        self._flaky_channel(panel, 6, lambda u: _r(302, u, headers={"location": "http://p.test/video/black.ts"}))
        got, ended = await _stream(panel, segments=12, recording=True)
        self.assertFalse(ended)
        # (upstream's #140 follows the redirect and refuses the clip by name; it is never sent)

    async def test_gone_a_404_channel_still_ends(self):
        """A viewer: after 3 re-resolves. A recording: after _GONE_GRACE of
        unbroken 404 (review round 2: a 7.5 s blip must not end it)."""
        panel = Panel(ttl=3)
        self._flaky_channel(panel, 10 ** 9, lambda u: _r(404, u))
        got, ended = await _stream(panel, segments=50, recording=False)
        self.assertTrue(ended)
        panel = Panel(ttl=3)
        self._flaky_channel(panel, 10 ** 9, lambda u: _r(404, u))
        with patch.object(livetv, "_GONE_GRACE", 0.3):
            got, ended = await _stream(panel, segments=50, recording=True)
        self.assertTrue(ended, "a channel gone for good must still end a recording")

    async def test_backlog_a_renumbered_window_is_not_cut(self):
        # The new token's numbering restarts at 0 a few segments back (a
        # backlog), well below the last segment sent: not the same numbering,
        # nothing may be skipped. (A restart that happens to land exactly
        # around the last number sent cannot be told apart without
        # #EXT-X-PROGRAM-DATE-TIME / a discontinuity sequence.)
        panel = Panel(ttl=6, renumber=True)
        got, ended = await _stream(panel, segments=24)
        holes = [n for n in range(min(got), max(got)) if n not in got]
        self.assertEqual([], holes, f"content deleted by the sequence dedupe: {got}")

    async def test_resolve_ts_a_stream_answer_is_never_read(self):
        reads = []

        class _Endless(httpx.AsyncByteStream):
            async def __aiter__(self):
                reads.append(1)
                while True:
                    await asyncio.sleep(0)    # like a socket: let the loop run
                    yield b"G" * 188

        panel = Panel(ttl=3)
        self._flaky_channel(panel, 10 ** 9, lambda u: httpx.Response(
            200, headers=TS, stream=_Endless(), request=httpx.Request("GET", u)))
        got, ended = await _stream(panel, segments=50, budget=0.5)
        self.assertEqual([], reads, "a raw stream was read as a playlist")
        self.assertTrue(ended, "a viewer's budget still ends it")



class ReviewRound2(unittest.IsolatedAsyncioTestCase):
    """Round-2 review of 73d623e (token_harness knobs BLACK_UNTIL, BLIP,
    SEQ_SHIFT and the requests-per-second report)."""

    def _flaky_channel(self, panel, bad, answer):
        return ReResolveReview._flaky_channel(self, panel, bad, answer)

    async def test_m1_a_placeholder_while_re_resolving_is_backed_off(self):
        """BLACK_UNTIL: 9,384 requests in 30 s, peaking at 900/s, with no
        sleep between them. Real time here (a fast fake sleep hides it):
        3 s against a channel URL that keeps redirecting to black.ts."""
        import time as _time
        panel = Panel(ttl=1)
        stamps = []

        def black(u):
            stamps.append(_time.monotonic())
            return _r(302, u, headers={"location": "http://p.test/video/black.ts"})
        self._flaky_channel(panel, 10 ** 9, black)
        livetv._recent_streams.clear()
        with patch("httpx.AsyncClient", lambda **kw: _Client(panel)), \
                patch("routers.livetv.is_safe_url", lambda *a, **k: True), \
                patch.object(livetv, "SessionLocal", side_effect=RuntimeError("no db")):
            resp = await livetv._stream_proxy_inner(
                channel_id=7, user_agent="UA", stream_url=CHANNEL, _release_sem=lambda: None,
                failure_budget=120.0, is_recording=lambda: True)

            async def collect():
                async for _ in resp.body_iterator:
                    pass
            try:
                await asyncio.wait_for(collect(), timeout=3.0)
            except asyncio.TimeoutError:
                pass
            await resp.body_iterator.aclose()
        per_second = {}
        for t in stamps:
            per_second[int(t)] = per_second.get(int(t), 0) + 1
        self.assertLessEqual(len(stamps), 6, f"{len(stamps)} placeholder re-resolves in 3 s")
        self.assertLessEqual(max(per_second.values() or [0]), 3, per_second)
        # (upstream's #140 follows the redirect and refuses the clip by name; it is never sent)

    async def test_blip_a_short_404_does_not_end_a_recording(self):
        """BLIP=6.5,14: a 7.5 s 404 blip ended a recording at 10.7 s."""
        panel = Panel(ttl=3)
        real = panel.route
        state = {"n": 0}

        def route(url):
            # after the first token expires, the panel 404s everything for a while
            if panel.issued >= 1 and state["n"] < 12 and (url == CHANNEL or url.endswith("index.m3u8")) \
                    and panel.tokens.get("T1", 1) <= 0:
                state["n"] += 1
                panel.log.append(url)
                return _r(404, url)
            return real(url)
        panel.route = route
        got, ended = await _stream(panel, segments=12, recording=True)
        self.assertFalse(ended, "a 404 blip ended the recording")
        self.assertTrue(any(d >= 0.5 for d in SLEPT))

    async def test_seq_shift_minus_one_loses_no_segment(self):
        """SEQ_SHIFT=-1: a new token's numbers are one lower; a skip by number
        silently dropped one real segment per re-resolve."""
        panel = Panel(ttl=3, seq_shift=-1)
        got, ended = await _stream(panel, segments=16)
        holes = [n for n in range(min(got), max(got)) if n not in got]
        self.assertEqual([], holes, got)

    async def test_seq_shift_plus_six_counts_no_phantom_skips(self):
        """SEQ_SHIFT=+6 counted 18 phantom segments_skipped."""
        panel = Panel(ttl=3, seq_shift=6)
        got, ended = await _stream(panel, segments=16)
        await asyncio.sleep(0)
        # the stream is closed by _stream; its summary is the last one
        self.assertEqual(0, livetv._recent_streams[-1]["segments_skipped"])

    async def test_t3_a_recording_whose_channel_turned_into_a_stream_ends(self):
        """Decided design: for a recording, a channel URL that keeps answering
        with a non-playlist stream ends the response after
        _STREAM_ANSWER_LIMIT, so Jellyfin re-opens on it (a second file with
        the content, instead of nothing until the timer ends)."""
        panel = Panel(ttl=3)
        self._flaky_channel(panel, 10 ** 9, lambda u: _r(200, u, b"G" * 188, TS))
        with patch.object(livetv, "_STREAM_ANSWER_LIMIT", 0.3):
            got, ended = await _stream(panel, segments=50, recording=True)
        self.assertTrue(ended)
        self.assertTrue(livetv._recent_streams[-1]["ended_on_error"])


if __name__ == "__main__":
    unittest.main()
