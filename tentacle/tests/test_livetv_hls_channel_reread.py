"""A live HLS channel is re-read from its channel URL, from near the live
edge, and switches to the continuous stream when its HLS fails.

Seen 2026-10-26 on one IPTV Bears account: the channel URL
(/live/<u>/<p>/<id>.m3u8) redirects to a one-time tokenized address on one
of ~80 edge servers. Re-reading THAT address answers 509 from the second read
on, which is how #700's HLS lost two of three channels within 30 s. Asked
from the channel URL every time (a fresh redirect, a fresh token), 20
different channels ran at once with no 509. Every read names every segment
anew (/hls/<token>/<id>_<seq>.ts), so what was sent is known by media
sequence (and file name), never by URL: a reader that keyed by URL fetched
the same segments again and again (~15 GB in 5 minutes).

The window is six 10 s segments: starting at its top put the picture a
minute behind live, so a viewer starts on the last segments that hold
_HLS_LIVE_START_SECONDS; a recording still takes the whole window.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import asyncio
import unittest
from unittest.mock import patch

import httpx

import routers.livetv as livetv

CHANNEL = "http://panel.test/live/u/p/414165.m3u8"
TS_URL = "http://panel.test/live/u/p/414165.ts"
PL = {"content-type": "application/x-mpegURL"}
TSH = {"content-type": "video/mp2t"}


def _r(status, url, content=b"", headers=None):
    return httpx.Response(status, headers=headers or {}, content=content,
                          request=httpx.Request("GET", url))


def _packet(tag: bytes) -> bytes:
    return b"G" + tag[:4].ljust(4, b"_") + b"\x00" * 183


class Bears:
    """A panel like IPTV Bears. The live window holds `window` segments and
    moves one segment every `reads_per_segment` playlist reads. The channel
    URL answers 302 to a fresh token on a fresh edge; a token's playlist read
    a second time answers 509. `lag` = {read number: segments behind} serves
    an edge that lags; `renumber_at` restarts the numbering (and names) at
    that read; `stall_after` stops the window moving after that many reads;
    `channel_status_after` = (read, status): the channel URL answers that
    from then on."""

    def __init__(self, window=6, reads_per_segment=2, start=250, lag=None, renumber_at=None,
                 stall_after=None, channel_status_after=None, duration=10.0):
        self.window, self.rps, self.start = window, reads_per_segment, start
        self.lag, self.renumber_at = lag or {}, renumber_at
        self.stall_after, self.channel_status_after = stall_after, channel_status_after
        self.duration = duration
        self.reads = 0            # playlist reads served
        self.issued = 0
        self.used = set()         # tokens whose playlist was read
        self.log = []
        self.refused = 0          # 509s answered

    def _newest(self):
        r = self.reads if self.stall_after is None else min(self.reads, self.stall_after)
        return self.start + r // self.rps

    def route(self, url):
        self.log.append(url)
        if url == CHANNEL:
            if self.channel_status_after and self.reads >= self.channel_status_after[0]:
                return _r(self.channel_status_after[1], url)
            self.issued += 1
            return _r(302, url, headers={"location": f"http://e{self.issued}.test/T{self.issued}/index.m3u8"})
        if url == TS_URL:
            return _r(200, url, b"".join(_packet(b"TS") for _ in range(64)), TSH)
        parts = url.split("/")
        tok = parts[3]
        if url.endswith("index.m3u8"):
            if tok in self.used:
                self.refused += 1
                return _r(509, url)
            self.used.add(tok)
            self.reads += 1
            newest = self._newest() - self.lag.get(self.reads, 0)
            prefix = "414165"
            first_n = max(self.start, newest - self.window + 1)
            shown = list(range(first_n, newest + 1))
            if self.renumber_at is not None and self.reads >= self.renumber_at:
                # the encoder restarted: numbering from 0, new names
                base = self._newest() - (self.window - 1)
                shown = [n - base for n in shown if n >= base]
                prefix = "414165r"
            lines = ["#EXTM3U", "#EXT-X-VERSION:3", f"#EXT-X-MEDIA-SEQUENCE:{shown[0]}",
                     "#EXT-X-TARGETDURATION:11"]
            for n in shown:
                lines += [f"#EXTINF:{self.duration},", f"/hls/{tok}{n}/{prefix}_{n}.ts"]
            return _r(200, url, ("\n".join(lines) + "\n").encode(), PL)
        if url.endswith(".ts") and "/hls/" in url:
            name = parts[-1][:-3]
            n = int(name.split("_")[1])
            tag = (b"R" if "r_" in name else b"") + f"{n:03d}".encode()
            return _r(200, url, _packet(tag), TSH)
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
        await asyncio.sleep(0)
        return self.panel.route(str(request.url))


async def _stream(panel, items=12, recording=False, fallback=None, budget=120.0, timeout=10.0):
    """What the client receives: one item per 188-byte packet, an int for
    an HLS segment's number ("R5" for a renumbered one), "TS" for the
    continuous stream."""
    real_sleep = asyncio.sleep

    async def fast_sleep(d):
        await real_sleep(0)

    livetv._recent_streams.clear()
    got = []
    with patch("httpx.AsyncClient", lambda **kw: _Client(panel)), \
            patch("routers.livetv.is_safe_url", lambda *a, **k: True), \
            patch("asyncio.sleep", fast_sleep), \
            patch.object(livetv, "SessionLocal", side_effect=RuntimeError("no db")):
        resp = await livetv._stream_proxy_inner(
            channel_id=41, user_agent="UA", stream_url=CHANNEL, _release_sem=lambda: None,
            guard=lambda *a, **k: True, failure_budget=budget, is_recording=lambda: recording,
            fallback_url=fallback)

        async def collect():
            async for piece in resp.body_iterator:
                for i in range(0, len(piece) - 187, 188):
                    tag = piece[i + 1:i + 5].rstrip(b"_").decode()
                    got.append(tag if not tag.isdigit() else int(tag))
                if len(got) >= items:
                    return
        ended = False
        try:
            await asyncio.wait_for(collect(), timeout=timeout)
            ended = len(got) < items
        except asyncio.TimeoutError:
            pass
        await resp.body_iterator.aclose()
    return got, ended


def _consecutive(nums):
    return all(b == a + 1 for a, b in zip(nums, nums[1:]))


class TheChannelUrlEveryTime(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        livetv._hls_failed_channels.clear()
        self.addCleanup(livetv._hls_failed_channels.clear)

    async def test_every_read_asks_the_channel_url_and_no_segment_goes_twice(self):
        panel = Bears()
        got, ended = await _stream(panel, items=15)
        self.assertFalse(ended)
        self.assertTrue(_consecutive(got), f"repeated or missing segments: {got}")
        self.assertEqual(0, panel.refused, "a tokenized playlist address was read again (509)")
        self.assertGreaterEqual(panel.log.count(CHANNEL), panel.reads)

    async def test_each_segment_is_fetched_once_although_every_read_renames_it(self):
        panel = Bears()
        await _stream(panel, items=12)
        fetched = [u.rsplit("/", 1)[-1] for u in panel.log if "/hls/" in u]
        self.assertEqual(len(fetched), len(set(fetched)), f"fetched again: {fetched}")

    async def test_a_viewer_starts_near_the_live_edge(self):
        panel = Bears(start=250, reads_per_segment=1)
        panel.reads = 10         # live a while: the open's read shows 256..261
        got, _ = await _stream(panel, items=4)
        first_window_newest = 250 + 11
        start = livetv._hls_start_index([10.0] * 6)
        self.assertEqual(4, start, "12 s of 10 s segments = the last two")
        self.assertEqual(first_window_newest - 1, got[0], f"not started at the live edge: {got}")
        self.assertTrue(_consecutive(got))

    async def test_a_recording_takes_the_whole_window(self):
        panel = Bears(start=250, reads_per_segment=1)
        panel.reads = 10
        got, _ = await _stream(panel, items=8, recording=True)
        self.assertEqual(256, got[0], f"a recording lost the window's start: {got}")
        self.assertTrue(_consecutive(got))

    async def test_an_edge_that_lags_sends_nothing_again(self):
        panel = Bears(reads_per_segment=1, lag={4: 1, 5: 2, 9: 1, 12: 3})
        panel.reads = 8
        got, ended = await _stream(panel, items=14)
        self.assertFalse(ended)
        self.assertTrue(_consecutive(got), f"a lagging edge put segments out twice or out of order: {got}")

    async def test_a_restarted_numbering_goes_on_from_its_live_edge(self):
        panel = Bears(reads_per_segment=1, renumber_at=14)
        panel.reads = 8
        got, ended = await _stream(panel, items=16)
        self.assertFalse(ended)
        before = [g for g in got if isinstance(g, int)]
        after = [g for g in got if isinstance(g, str) and g.startswith("R")]
        self.assertTrue(_consecutive(before), got)
        self.assertTrue(after, f"the restarted numbering was never sent: {got}")
        renumbered = [int(g[1:]) for g in after]
        self.assertTrue(_consecutive(renumbered), got)
        # a viewer goes on from the new numbering's live edge, not its top
        self.assertEqual(livetv._hls_start_index([10.0] * 6), renumbered[0], got)

    async def test_a_viewer_whose_channel_url_keeps_refusing_still_ends(self):
        panel = Bears(reads_per_segment=1, channel_status_after=(8, 407))
        panel.reads = 2
        got, ended = await _stream(panel, items=200)
        self.assertTrue(ended)
        self.assertLessEqual(panel.log.count(CHANNEL) - panel.issued, 2 + livetv._MAX_RERESOLVE)

    async def test_a_redirect_free_channel_is_read_as_before(self):
        """No redirect: the playlist URL is the channel URL; nothing changes."""
        panel = Bears(reads_per_segment=1)
        orig = panel.route

        def route(url):
            if url == CHANNEL:
                panel.log.append(url)
                panel.issued += 1
                r = orig(f"http://e{panel.issued}.test/T{panel.issued}/index.m3u8")
                return _r(r.status_code, url, r.content, dict(r.headers))
            return orig(url)
        panel.route = route
        panel.reads = 8
        got, ended = await _stream(panel, items=10)
        self.assertFalse(ended)
        self.assertTrue(_consecutive(got), got)


class SwitchToTheContinuousStream(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        livetv._hls_failed_channels.clear()
        self.addCleanup(livetv._hls_failed_channels.clear)

    async def test_a_stalled_feed_goes_on_as_ts_in_the_same_response(self):
        # Seen on Nick: one segment, then the same window for good
        panel = Bears(reads_per_segment=1, stall_after=9)
        panel.reads = 8
        with patch.object(livetv, "_HLS_STALL_SECONDS", 0.2), patch.object(livetv, "_HLS_STALL_TARGETS", 0):
            got, ended = await _stream(panel, items=40, fallback=TS_URL)
        self.assertFalse(ended)
        hls = [g for g in got if isinstance(g, int)]
        self.assertTrue(hls and _consecutive(hls), got)
        self.assertEqual("TS", got[-1], f"never switched to the continuous stream: {got[:20]}")
        first_ts = got.index("TS")
        self.assertTrue(all(g == "TS" for g in got[first_ts:]))
        self.assertIn(41, livetv._hls_failed_channels, "the next open should take ts first")
        hls_summary = [s for s in livetv._recent_streams if s.get("hls")]
        self.assertTrue(hls_summary and hls_summary[0]["switched_to_ts"])
        self.assertFalse(hls_summary[0]["ended_on_error"])

    async def test_without_a_fallback_a_stalled_feed_keeps_being_read(self):
        panel = Bears(reads_per_segment=1, stall_after=9)
        panel.reads = 8
        with patch.object(livetv, "_HLS_STALL_SECONDS", 0.2), patch.object(livetv, "_HLS_STALL_TARGETS", 0):
            got, ended = await _stream(panel, items=40, timeout=1.0)
        self.assertNotIn("TS", got)
        self.assertNotIn(41, livetv._hls_failed_channels)

    async def test_an_hls_that_ends_on_errors_goes_on_as_ts(self):
        panel = Bears(reads_per_segment=1, channel_status_after=(8, 404))
        panel.reads = 2
        got, ended = await _stream(panel, items=60, fallback=TS_URL)
        self.assertFalse(ended)
        self.assertIn("TS", got)
        self.assertIn(41, livetv._hls_failed_channels)

    async def test_an_hls_open_refused_outright_opens_ts_and_is_remembered(self):
        panel = Bears()
        orig = panel.route
        panel.route = lambda url: _r(404, url) if url == CHANNEL else orig(url)
        got, ended = await _stream(panel, items=10, fallback=TS_URL)
        self.assertEqual(["TS"] * 10, got[:10])
        self.assertIn(41, livetv._hls_failed_channels)


class WindowPlan(unittest.TestCase):
    def test_what_follows_the_last_one_sent(self):
        seqs, names = [253, 254, 255, 256, 257, 258], [f"x_{n}.ts" for n in range(253, 259)]
        self.assertEqual((4, "continue", 0), livetv._hls_window_plan(seqs, names, 256, "x_256.ts"))
        self.assertEqual((6, "continue", 0), livetv._hls_window_plan(seqs, names, 258, "x_258.ts"))

    def test_a_counter_one_off_is_set_right_by_the_name(self):
        seqs, names = [253, 254, 255, 256, 257, 258], [f"x_{n}.ts" for n in range(252, 258)]
        # the number says 256 was sent; by name it is the one numbered 257
        self.assertEqual((5, "continue", 0), livetv._hls_window_plan(seqs, names, 256, "x_256.ts"))

    def test_names_reused_in_a_ring_never_overrule_numbers(self):
        seqs, names = [10, 11, 12, 13, 14, 15], ["s0.ts", "s1.ts", "s2.ts", "s3.ts", "s4.ts", "s5.ts"]
        self.assertEqual((2, "continue", 0), livetv._hls_window_plan(seqs, names, 11, "s5.ts"))

    def test_a_window_that_moved_on_counts_what_was_lost(self):
        seqs, names = [260, 261, 262], ["a.ts", "b.ts", "c.ts"]
        self.assertEqual((0, "gap", 3), livetv._hls_window_plan(seqs, names, 256, "z.ts"))
        self.assertEqual((0, "gap", 0), livetv._hls_window_plan(seqs, names, 256, "z.ts", same_disc=False))

    def test_a_lagging_edge_is_behind(self):
        seqs, names = [250, 251, 252, 253, 254, 255], [f"x_{n}.ts" for n in range(250, 256)]
        self.assertEqual((6, "behind", 0), livetv._hls_window_plan(seqs, names, 257, "x_257.ts"))
        self.assertEqual((6, "behind", 0), livetv._hls_window_plan(
            seqs, names, 260, "x_260.ts", sent_names=["x_255.ts", "x_256.ts"]))

    def test_numbering_from_scratch_is_a_restart(self):
        self.assertEqual((0, "restart", 0), livetv._hls_window_plan(
            [0, 1, 2], ["r_0.ts", "r_1.ts", "r_2.ts"], 257, "x_257.ts"))

    def test_a_renumbered_window_goes_on_by_name(self):
        self.assertEqual((2, "continue", 0), livetv._hls_window_plan(
            [0, 1, 2, 3], ["x_255.ts", "x_256.ts", "x_257.ts", "x_258.ts"], 257, "x_256.ts"))


class NextRead(unittest.TestCase):
    def test_just_after_the_next_segment_is_due(self):
        # read at 100, the newest turned up ~98 and lasts 10 s: due 108
        self.assertEqual(108.5, livetv._hls_next_read(100.0, 108.0, 11))

    def test_then_every_quarter_target_until_it_is_listed(self):
        self.assertEqual(102.0, livetv._hls_next_read(100.0, 99.0, 11), "due already: poll in 2 s")
        self.assertEqual(101.0, livetv._hls_next_read(100.0, 99.0, 2), "short segments: at least 1 s")
        self.assertEqual(102.0, livetv._hls_next_read(100.0, None, 11), "nothing known yet")

    def test_a_segment_still_owed_is_asked_for_soon(self):
        self.assertEqual(102.0, livetv._hls_next_read(100.0, 108.0, 11, owed=True))

    def test_never_later_than_a_target_duration(self):
        self.assertEqual(111.0, livetv._hls_next_read(100.0, 130.0, 11))

    def test_few_reads_per_segment_on_a_steady_panel(self):
        """A panel that lists a 10 s segment every 10 s (at t = 3, 13, 23 ...):
        about 1.5 reads per segment, each segment seen within ~2.5 s."""
        t, newest_listed, prev, due, reads, late = 0.0, None, None, None, 0, []
        appeared_at = {}
        while t < 600:
            reads += 1
            newest = int((t - 3) // 10) if t >= 3 else -1
            if newest >= 0 and (newest_listed is None or newest > newest_listed):
                appeared = t if prev is None or newest_listed is None else t - min(t - prev, livetv._hls_poll(11)) / 2
                due = appeared + 10.0
                for n in range((newest_listed if newest_listed is not None else newest - 1) + 1, newest + 1):
                    appeared_at[n] = t
                    late.append(t - (3 + 10 * n))
                newest_listed = newest
            prev = t
            t = livetv._hls_next_read(t, due, 11)
        self.assertLessEqual(reads / len(appeared_at), 1.8, f"{reads} reads for {len(appeared_at)} segments")
        self.assertLessEqual(max(late[3:]), 2.6, f"seen late: {late}")


class ConnectsToTheApprovedAddress(unittest.IsolatedAsyncioTestCase):
    """The HLS client connects to the address the guard approved for a host
    (no second lookup: each one-time host is a query at the home's resolver).
    The suite has no DNS at all, so a fetch that works proves it."""

    async def test_an_approved_host_is_reached_without_a_lookup(self):
        from services import ssrf

        async def handle(reader, writer):
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
            await writer.drain()
            writer.close()
        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        self.addAsyncCleanup(server.wait_closed)
        self.addCleanup(server.close)
        ssrf._approve("edge-one-time.example", ["127.0.0.1"])
        self.addCleanup(ssrf._approved.pop, "edge-one-time.example", None)
        async with httpx.AsyncClient(transport=livetv._approved_address_transport()) as client:
            r = await client.get(f"http://edge-one-time.example:{port}/x")
        self.assertEqual(b"ok", r.content)

    async def test_without_an_approval_the_name_is_looked_up_as_ever(self):
        async with httpx.AsyncClient(transport=livetv._approved_address_transport()) as client:
            with self.assertRaises(httpx.ConnectError):
                await client.get("http://never-approved.example:9/x")

    def test_an_approval_is_kept_briefly(self):
        from services import ssrf
        ssrf._approve("Brief.example", ["93.184.216.34"])
        self.addCleanup(ssrf._approved.pop, "brief.example", None)
        self.assertEqual(["93.184.216.34"], ssrf.approved_ips("brief.example"))
        ssrf._approved["brief.example"] = (["93.184.216.34"], 0.0)
        self.assertIsNone(ssrf.approved_ips("brief.example"))

    def test_a_lan_providers_origin_is_never_remembered(self):
        import socket
        from unittest.mock import patch as p
        from services import ssrf
        self.addCleanup(ssrf._approved.pop, "tuliprox.lan", None)

        def gai(host, *a, **k):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.20", 0))]
        with p.object(ssrf.socket, "getaddrinfo", gai):
            guard = ssrf.lan_origin_guard("http://tuliprox.lan:8901")
            self.assertTrue(guard("http://tuliprox.lan:8901/live/u/p/1.m3u8"))
        self.assertIsNone(ssrf.approved_ips("tuliprox.lan"))

    def test_a_public_verdict_is_what_gets_approved(self):
        import socket
        from unittest.mock import patch as p
        from services import ssrf
        self.addCleanup(ssrf._approved.pop, "cdn.example", None)
        self.addCleanup(ssrf._approved.pop, "inside.example", None)
        answers = {"cdn.example": "93.184.216.34", "inside.example": "10.0.0.5"}

        def gai(host, *a, **k):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (answers[host], 0))]
        with p.object(ssrf.socket, "getaddrinfo", gai):
            self.assertTrue(ssrf.is_safe_url("http://cdn.example/x"))
            self.assertFalse(ssrf.is_safe_url("http://inside.example/x"))
        self.assertEqual(["93.184.216.34"], ssrf.approved_ips("cdn.example"))
        self.assertIsNone(ssrf.approved_ips("inside.example"))


class StartSetting(unittest.TestCase):
    def test_the_setting_or_the_default(self):
        from types import SimpleNamespace
        from unittest.mock import patch as p
        for raw, want in (("", livetv._HLS_LIVE_START_SECONDS), ("0", 0.0), ("25", 25.0),
                          ("-3", 0.0), ("soon", livetv._HLS_LIVE_START_SECONDS)):
            with p.object(livetv, "get_setting", lambda db, k, d="": raw):
                self.assertEqual(want, livetv._hls_start_seconds(SimpleNamespace()), raw)


class SegmentPieces(unittest.IsolatedAsyncioTestCase):
    """A segment is handed on in packet-aligned pieces, never one 5-8 MB
    bytes object (those fragmented glibc's heap until the process grew
    without bound)."""

    async def test_whole_packets_in_pieces_and_nothing_lost(self):
        body = bytes(range(256)) * (188 * 3000 // 256 + 1)
        body = body[:188 * 3000 + 100]          # a ragged end, as a provider may send

        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                for i in range(0, len(body), 65536 + 7):     # odd network chunks
                    yield body[i:i + 65536 + 7]
        r = httpx.Response(200, stream=Stream(), request=httpx.Request("GET", "http://e.test/1.ts"))
        pieces = await livetv._aread_within(r, 10.0, None, pieces=True)
        self.assertEqual(body, b"".join(pieces))
        self.assertTrue(all(len(p) == livetv._HLS_PIECE for p in pieces[:-1]))
        self.assertLessEqual(len(pieces[-1]), livetv._HLS_PIECE)
        self.assertEqual(0, livetv._HLS_PIECE % 188)


class StartIndex(unittest.TestCase):
    def test_the_last_segments_that_hold_the_start(self):
        self.assertEqual(4, livetv._hls_start_index([10.0] * 6, 12.0))
        self.assertEqual(5, livetv._hls_start_index([10.0] * 6, 10.0))
        self.assertEqual(5, livetv._hls_start_index([10.0] * 6, 0.0), "one segment at least")
        self.assertEqual(0, livetv._hls_start_index([2.0] * 3, 12.0), "a short window: all of it")
        self.assertEqual(4, livetv._hls_start_index([2.0] * 10, 12.0))


if __name__ == "__main__":
    unittest.main()
