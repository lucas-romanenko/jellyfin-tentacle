"""A re-dialled raw MPEG-TS connection must not write the provider's replay twice (#368).

Run from the tentacle/ directory:  python -m unittest discover -s tests

Many Xtream panels start every (re)opened raw TS connection with their buffer,
about 20 s behind live: packets an earlier connection already delivered, byte
for byte. The re-dial forwarded all of it, so on an account that closes the
older of two connections every ~13 s a recording grew 1.7-2.6x and jumped back
~22 s at every reconnect, and every recording was reported as damaged.

The fix joins a re-dialled connection right after the last byte sent, but only
when that is proven; anything it cannot prove goes out as before (duplicates
at worst). Every test here also checks that nothing the provider delivered is
lost.
"""
import asyncio
import random
import unittest
from collections import Counter
from unittest.mock import MagicMock, patch

import httpx

import routers.livetv as livetv
from test_livetv_open_single_fetch import PANEL, TOKENIZED, FakeClient, _redirect, _resp
from test_livetv_raw_reconnect import _dropped, _live, _play

PAT = bytes([0x47, 0x40, 0x00, 0x10]) + b"\x00\x00\xb0\x0d" + b"\xff" * 180   # the same every time
NULL = bytes([0x47, 0x1F, 0xFF, 0x10]) + b"\xff" * 184


def _pts(v: int) -> bytes:
    return bytes([0x21 | ((v >> 29) & 0x0E), (v >> 22) & 0xFF, ((v >> 14) & 0xFE) | 1,
                  (v >> 7) & 0xFF, ((v << 1) & 0xFE) | 1])


def pkt(n: int) -> bytes:
    """TS packet n of the channel: PID 0x100, a video frame start (PES with a PTS) carrying n."""
    head = bytes([0x47, 0x41, 0x00, 0x10 | (n & 0x0F)])
    body = b"\x00\x00\x01\xe0\x00\x00\x80\x80\x05" + _pts(n * 3600) + n.to_bytes(8, "big")
    return head + body + b"\xff" * (188 - len(head) - len(body))


def mixed(n: int) -> bytes:
    """Packet n of a realistic mux: a frame start every 10, tables and null
    packets (identical each time), the rest a frame's middle (no PUSI)."""
    if n % 10 == 0:
        return pkt(n)
    if n % 10 == 5:
        return PAT
    if n % 10 == 7:
        return NULL
    head = bytes([0x47, 0x01, 0x00, 0x10 | (n & 0x0F)])
    return head + n.to_bytes(8, "big") + b"\xab" * (188 - 12)


def packets(data: bytes) -> list:
    return [int.from_bytes(data[i + 18:i + 26], "big") for i in range(0, len(data) - 187, 188)]


def numbered(data: bytes) -> list:
    """The numbers of a `mixed` stream's packets, tables and null packets left out."""
    out = []
    for i in range(0, len(data) - 187, 188):
        p = data[i:i + 188]
        if p not in (PAT, NULL):
            out.append(int.from_bytes(p[18:26] if p[1] & 0x40 else p[4:12], "big"))
    return out


def connection(first: int, last: int, then_drop=True, make=pkt, piece=188 * 7, skip=0):
    """One provider connection delivering packets first..last-1 (in pieces of `piece` bytes)."""
    data = b"".join(make(n) for n in range(first, last))[skip:]
    pieces = [data[i:i + piece] for i in range(0, len(data), piece)]
    return _live(pieces, then=_dropped() if then_drop else None)


def scripted(conns):
    return {PANEL: [_redirect()] * len(conns) + [_resp(404, PANEL)], TOKENIZED: conns}


def pingpong(reconnects: int, replay: int, fresh: int):
    """Connection k delivers `replay` packets already sent, then `fresh` new ones, then drops."""
    conns, pos = [], 0
    for k in range(reconnects + 1):
        start = max(0, pos - replay) if k else 0
        conns.append(connection(start, pos + fresh, then_drop=k < reconnects))
        pos += fresh
    return scripted(conns), pos


class RawRedialReplay(unittest.IsolatedAsyncioTestCase):
    async def _check(self, reconnects, replay, fresh):
        script, total = pingpong(reconnects, replay, fresh)
        body, *_ = await _play(script)
        got = packets(body)
        counts = Counter(got)
        repeated = sum(c - 1 for c in counts.values())
        back = sum(1 for a, b in zip(got, got[1:]) if b < a)
        self.assertEqual(set(range(total)), set(counts), "content was lost")
        self.assertTrue(
            got == list(range(total)),
            f"{len(got)} packets written for {total} unique ({len(got) / total:.2f}x), "
            f"{repeated} repeated, {back} backward jumps over {reconnects} reconnects")

    async def test_one_redial_with_a_replay_writes_each_packet_once(self):
        await self._check(reconnects=1, replay=22, fresh=100)

    async def test_pingpong_every_13_with_a_22_replay_writes_each_packet_once(self):
        await self._check(reconnects=20, replay=22, fresh=13)


class Splice(unittest.IsolatedAsyncioTestCase):
    async def _body(self, conns):
        body, *_ = await _play(scripted(conns))
        self.assertEqual(0, len(body) % 188, "only whole packets go out")
        return body

    async def test_a_real_gap_is_sent_whole(self):
        """The new connection starts past what was sent: nothing to drop."""
        got = packets(await self._body([connection(0, 100), connection(150, 200, then_drop=False)]))
        self.assertEqual(list(range(100)) + list(range(150, 200)), got)

    async def test_a_seamless_redial_is_sent_whole(self):
        got = packets(await self._body([connection(0, 100), connection(100, 200, then_drop=False)]))
        self.assertEqual(list(range(200)), got)

    async def test_a_replay_that_reaches_back_over_an_earlier_gap_loses_nothing(self):
        """0-99 sent, a gap, 150-199 sent; the next connection replays from 90.
        Joining on the last frame sent (199) would drop 100-149, never sent."""
        got = packets(await self._body([connection(0, 100), connection(150, 200),
                                        connection(90, 250, then_drop=False)]))
        self.assertEqual(set(range(250)), set(got), "content was lost")
        self.assertEqual(list(range(90, 250)), got[-160:])

    async def test_what_precedes_the_first_frame_of_a_replay_is_not_dropped_if_never_sent(self):
        """0-99 sent, a gap, 120-199 sent (it starts on a frame); the next
        connection replays from 116. Its first frame (120) was sent, but
        116-119 in front of it never were."""
        body = await self._body([connection(0, 100, make=mixed), connection(120, 200, make=mixed),
                                 connection(116, 300, then_drop=False, make=mixed)])
        sent = {n for n in [*range(100), *range(116, 300)] if n % 10 not in (5, 7)}
        self.assertEqual(sent, set(numbered(body)), "content was lost")

    async def test_a_replay_in_a_mux_with_tables_and_null_packets_is_joined(self):
        """Tables and null packets repeat by themselves: no evidence either way."""
        body = await self._body([connection(0, 1000, make=mixed),
                                 connection(785, 2000, then_drop=False, make=mixed)])
        self.assertEqual(b"".join(mixed(n) for n in range(2000)), body)

    async def test_a_replay_split_over_tiny_reads_is_joined(self):
        body = await self._body([connection(0, 300, make=mixed, piece=7),
                                 connection(211, 600, then_drop=False, make=mixed, piece=7)])
        self.assertEqual(b"".join(mixed(n) for n in range(600)), body)

    async def test_a_redial_that_starts_mid_packet_inside_the_replay_is_joined(self):
        """#335 trims the partial first packet; the rest is joined as usual."""
        body = await self._body([connection(0, 300, make=mixed),
                                 connection(200, 600, then_drop=False, make=mixed, skip=100)])
        self.assertEqual(b"".join(mixed(n) for n in range(600)), body)

    async def test_a_replay_that_is_not_byte_identical_is_sent_whole(self):
        """A remuxing restreamer: the frames differ, so nothing is proven."""
        def remuxed(n):
            p = mixed(n)
            return p[:-1] + b"\x00" if n % 10 else p     # frame starts identical, middles not
        conn2 = connection(250, 400, then_drop=False, make=lambda n: remuxed(n) if n < 300 else mixed(n))
        body = await self._body([connection(0, 300, make=mixed), conn2])
        want = (b"".join(mixed(n) for n in range(300)) +
                b"".join(remuxed(n) if n < 300 else mixed(n) for n in range(250, 400)))
        self.assertEqual(want, body)

    async def test_a_connection_that_breaks_during_its_replay_loses_nothing(self):
        got = packets(await self._body([connection(0, 100), connection(80, 95),
                                        connection(85, 200, then_drop=False)]))
        self.assertEqual(set(range(200)), set(got), "content was lost")
        self.assertEqual(list(range(100, 200)), got[-100:], "the last re-dial was not joined")

    async def test_a_replay_larger_than_the_hold_is_sent_whole(self):
        with patch.object(livetv, "_SPLICE_HOLD_BYTES", 188 * 50):
            got = packets(await self._body([connection(0, 100), connection(30, 200, then_drop=False)]))
        self.assertEqual(list(range(100)) + list(range(30, 200)), got)

    async def test_a_stream_without_frame_starts_is_sent_whole(self):
        def plain(n):
            return bytes([0x47, 0x01, 0x00, 0x10 | (n & 0x0F)]) + n.to_bytes(8, "big") + b"\x00" * 176
        body = await self._body([connection(0, 50, make=plain), connection(40, 80, then_drop=False, make=plain)])
        self.assertEqual(b"".join(plain(n) for n in list(range(50)) + list(range(40, 80))), body)

    async def test_random_histories_lose_nothing_and_pure_replays_repeat_nothing(self):
        rng = random.Random(368)
        for seed in range(150):
            conns, pos, sent, pure = [], 0, set(), True
            for k in range(rng.randint(1, 6)):
                if k:
                    kind = rng.choice(["replay", "replay", "gap", "seamless"])
                    if kind != "replay":
                        pure = False
                    start = (max(0, pos - rng.randint(10, 80)) if kind == "replay"
                             else pos + rng.randint(1, 40) if kind == "gap" else pos)
                else:
                    start = 0
                end = max(start + 4, pos + rng.randint(1, 120))
                skip = rng.choice([0, 0, rng.randint(1, 187)]) if k else 0
                if skip:
                    pure = False      # the trimmed packet is a frame start: may lose the anchor
                conns.append(connection(start, end, make=mixed, piece=rng.randint(1, 4000), skip=skip))
                sent |= set(range(start + (1 if skip else 0), end))
                pos = max(pos, end)
            conns[-1] = connection(start, end, then_drop=False, make=mixed, piece=500, skip=skip)
            body = await self._body(conns)
            got = set(numbered(body))
            want = {n for n in sent if n % 10 not in (5, 7)}
            self.assertEqual(want, got, f"seed {seed}: content lost or invented")
            if pure:
                self.assertEqual(b"".join(mixed(n) for n in range(pos)), body, f"seed {seed}: repeated")


class _StillLive(httpx.AsyncByteStream):
    """A connection that delivers its pieces and then stays open (live)."""

    def __init__(self, pieces):
        self._pieces = pieces

    async def __aiter__(self):
        for piece in self._pieces:
            yield piece
        await asyncio.Event().wait()

    async def aclose(self):
        pass


class SpliceHealth(unittest.IsolatedAsyncioTestCase):
    """A joined reconnect lost nothing: the recording is not reported as
    "may be missing content" for it (every one was before)."""

    def setUp(self):
        livetv._recent_streams.clear()

    async def _record(self, conns, last):
        """Record until packet `last[1]` of the last connection (live, open
        after `last[2]` packets) is out, then stop, as Jellyfin's timer does.
        The raw path sends ~128 KB pieces, so the connection holds more."""
        data = b"".join(pkt(n) for n in range(last[0], last[2]))
        conns = conns + [httpx.Response(200, headers={"content-type": "video/mp2t"},
                                        stream=_StillLive([data]), request=httpx.Request("GET", TOKENIZED))]
        writes, log, closed = [], [], []
        real_sleep = asyncio.sleep

        async def fast_sleep(delay):
            await real_sleep(0)
        with patch("httpx.AsyncClient", lambda **kw: FakeClient(scripted(conns), log, closed, **kw)), \
                patch("routers.livetv.is_safe_url", lambda *a, **k: True), \
                patch("asyncio.sleep", fast_sleep), \
                patch.object(livetv, "log_activity", lambda db, ev, msg, detail=None: writes.append(ev)), \
                patch.object(livetv, "SessionLocal", MagicMock()):
            resp = await livetv._stream_proxy_inner(
                channel_id=7, user_agent="UA", stream_url=PANEL, _release_sem=lambda: None,
                guard=None, is_recording=lambda: True)
            body = b""
            async for piece in resp.body_iterator:
                body += piece
                if packets(body)[-1:] and packets(body)[-1] >= last[1]:
                    break
            await resp.body_iterator.aclose()
            for _ in range(20):          # the Activity line is written from the executor
                if writes:
                    break
                await real_sleep(0.01)
        return packets(body)[:last[1]], livetv._recent_streams[-1], writes

    async def test_a_recording_whose_reconnects_were_all_joined_is_not_damaged(self):
        got, last, writes = await self._record([connection(0, 100), connection(80, 150)], (130, 200, 1200))
        self.assertEqual(list(range(200)), got)
        self.assertEqual((2, 2), (last["reconnects"], last["replays_joined"]))
        self.assertEqual((20 + 20) * 188, last["replay_bytes_skipped"])
        self.assertFalse(last["ended_on_error"])
        self.assertNotIn("livetv_recording_damaged", writes)

    async def test_a_reconnect_not_joined_is_still_damage(self):
        got, last, writes = await self._record([connection(0, 100), connection(80, 150)], (170, 200, 1200))
        self.assertEqual((2, 1), (last["reconnects"], last["replays_joined"]))
        self.assertIn("livetv_recording_damaged", writes)


if __name__ == "__main__":
    unittest.main()
