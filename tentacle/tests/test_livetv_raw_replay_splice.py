"""A re-dialled raw MPEG-TS stream goes on where it stopped, not ~22 s earlier.

Run from the tentacle/ directory:  python -m unittest discover -s tests

Measured on a live account whose provider closes the older of two connections
every ~13 s: each re-dialled connection starts with the provider's buffer,
about 22 s behind live, byte for byte the packets already forwarded. Proxied as
it came, a recording repeated ~22 s after every reconnect (files 2.5x the size,
playback jumping back). After a re-dial Tentacle now skips the fresh bytes up
to an exact match of what it sent since the last PES header before the drop --
and never drops a packet it has not proven was sent already.
"""
import asyncio
import random
import unittest
from unittest.mock import MagicMock, patch

from test_livetv_raw_reconnect import _live, _dropped, _play
from test_livetv_open_single_fetch import PANEL, TOKENIZED, _redirect, _resp

VIDEO = 0x100
NULL = bytes([0x47, 0x1F, 0xFF, 0x10]) + b"\xff" * 184


def pkt(n: int) -> bytes:
    """Video packet n: every 10th starts a PES (payload 00 00 01 e0), like real video."""
    pusi = n % 10 == 0
    head = bytes([0x47, 0x40 | (VIDEO >> 8) if pusi else (VIDEO >> 8), VIDEO & 0xFF, 0x10 | (n & 0x0F)])
    lead = b"\x00\x00\x01\xe0" if pusi else b"\xaa\xaa\xaa\xaa"
    return head + lead + n.to_bytes(8, "big") * 22 + b"\x00" * 4


def pat(cc: int) -> bytes:
    """A PAT: byte-identical every 16 repetitions, as in any real mux."""
    return (bytes([0x47, 0x40, 0x00, 0x10 | (cc & 0x0F)])
            + bytes([0, 0, 0xB0, 13, 0, 1, 0xC1, 0, 0, 0, 1, 0xF0, 0, 1, 2, 3, 4]) + b"\xff" * 167)


AUDIO = 0x101


def silence(n: int) -> bytes:
    """An audio packet: a PES start (unique PTS) every 4th, byte-identical silence
    otherwise -- content packets that repeat byte for byte (review round 2)."""
    if n % 4 == 0:
        return bytes([0x47, 0x40 | (AUDIO >> 8), AUDIO & 0xFF, 0x10 | ((n // 4) & 0x0F)]) \
            + b"\x00\x00\x01\xc0" + n.to_bytes(8, "big") + b"\x55" * 172
    return bytes([0x47, AUDIO >> 8, AUDIO & 0xFF, 0x10]) + b"\x55" * 184


def run(a: int, b: int, psi: bool = True, audio: bool = True) -> bytes:
    """Video packets a..b-1 in a typical CBR mux: a PAT every 20, a null every 8,
    an audio packet (mostly identical silence) every 3."""
    out = bytearray()
    for n in range(a, b):
        if psi and n % 20 == 0:
            out += pat(n // 20)
        if psi and n % 8 == 3:
            out += NULL
        if audio and n % 3 == 1:
            out += silence(n // 3)
        out += pkt(n)
    return bytes(out)


def numbers(body: bytes) -> list:
    assert len(body) % 188 == 0, "only whole packets may go downstream"
    return [int.from_bytes(body[i + 8:i + 16], "big") for i in range(0, len(body), 188)
            if ((body[i + 1] & 0x1F) << 8 | body[i + 2]) == VIDEO]


def pieces(data: bytes, rng=None, size=5000) -> list:
    out, i = [], 0
    while i < len(data):
        n = rng.randint(1, 9000) if rng else size
        out.append(data[i:i + n])
        i += n
    return out


async def until(condition, timeout=10.0):
    """Wait for a background write (the Activity entry goes through an executor)."""
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not condition() and loop.time() < end:
        await asyncio.sleep(0.01)


def script(conns, rng=None):
    return {PANEL: [_redirect()] * len(conns) + [_resp(404, PANEL)],
            TOKENIZED: [_live(pieces(c, rng), then=_dropped() if i < len(conns) - 1 else None)
                        for i, c in enumerate(conns)]}


class ReplayIsSpliced(unittest.IsolatedAsyncioTestCase):
    async def test_the_replayed_seconds_are_not_sent_twice(self):
        body, *_ = await _play(script([run(0, 1000), run(700, 1500)]))
        self.assertEqual(list(range(1500)), numbers(body),
                         "the provider's replay of already-sent packets reached the recording")

    async def test_without_tables_in_the_mux_too(self):
        body, *_ = await _play(script([run(0, 1000, False), run(700, 1500, False)]))
        self.assertEqual(list(range(1500)), numbers(body))

    async def test_a_real_gap_goes_out_at_once_in_order(self):
        body, *_ = await _play(script([run(0, 1000), run(1100, 1600)]))
        self.assertEqual(list(range(1000)) + list(range(1100, 1600)), numbers(body))

    async def test_an_anchor_split_over_several_reads_is_found(self):
        second = run(900, 1200)
        s = {PANEL: [_redirect(), _redirect(), _resp(404, PANEL)],
             TOKENIZED: [_live(pieces(run(0, 1000)), then=_dropped()),
                         _live([second[i:i + 7] for i in range(0, len(second), 7)])]}
        body, *_ = await _play(s)
        self.assertEqual(list(range(1200)), numbers(body))

    async def test_a_connection_that_breaks_during_the_replay(self):
        body, *_ = await _play(script([run(0, 1000), run(800, 900), run(850, 1400)]))
        got = numbers(body)
        self.assertTrue(set(range(1400)) <= set(got), "content lost")
        self.assertEqual(list(range(1400)), sorted(set(got)))

    async def test_a_connection_that_breaks_before_the_probe_completes(self):
        body, *_ = await _play(script([run(0, 1000), run(995, 1000), run(990, 1300)]))
        got = numbers(body)
        self.assertTrue(set(range(1300)) <= set(got), "content lost")

    async def test_a_seamless_redial_without_replay_loses_nothing(self):
        body, *_ = await _play(script([run(0, 1000), run(1000, 1300)]))
        self.assertEqual(list(range(1300)), numbers(body))

    async def test_a_stream_that_is_not_mpeg_ts_is_passed_through(self):
        body, *_ = await _play(script([b"AAAA" * 100, b"AAAA" * 100]))
        self.assertEqual(800, len(body))

    # --- review round 1 (B1): tables and null packets are not evidence ------
    async def test_gap_then_a_breaking_connection_loses_nothing(self):
        body, *_ = await _play(script([run(0, 1000), run(1100, 1600), run(1600, 1700)]))
        self.assertEqual([], sorted(set(range(1100, 1700)) - set(numbers(body))))

    async def test_gap_longer_than_the_buffer_then_ping_pong_loses_nothing(self):
        conns, live = [run(0, 1000)], 1500
        for _ in range(6):
            conns.append(run(max(1500, live - 300), live + 400))
            live += 400
        body, *_ = await _play(script(conns))
        got = numbers(body)
        self.assertEqual([], sorted(set(range(1500, live)) - set(got)))
        # within _SPLICE_AFTER_GAP of the gap nothing is joined: repeats, never loss

    async def test_a_channel_restart_is_sent_whole(self):
        restarted = run(0, 500)          # the provider restarted the channel: numbers start again
        body, *_ = await _play(script([run(5000, 6000), restarted]))
        self.assertEqual(list(range(5000, 6000)) + list(range(500)), numbers(body))

    async def test_a_null_packet_at_the_join_is_not_a_splice(self):
        import routers.livetv as livetv
        sp = livetv._ReplaySplicer(lambda: 0.0)
        sp.sent(run(0, 1000) + NULL)
        sp.redialled()
        out = sp.feed(NULL + run(1100, 1300)) + sp.broke()
        self.assertEqual(list(range(1100, 1300)), numbers(out))
        self.assertEqual((0, 1), (sp.splices, sp.misses), "a real gap must be counted as one")

    # --- review round 3 (S9b): repeating content packets never anchor a join ---
    def test_radio_silence_after_a_real_gap_is_sent_whole_and_reported(self):
        """Audio only, digital silence: long PES whose continuation packets repeat
        byte for byte every 16 (CC). The last 8 packets sent recur in the fresh
        connection after a real gap; v3 joined there, dropped new audio and
        reported a splice. The anchor now starts at a PES header."""
        import routers.livetv as livetv

        def radio(a, b):
            out = bytearray()
            for n in range(a, b):
                if n % 40 == 0:
                    out += (bytes([0x47, 0x40 | (AUDIO >> 8), AUDIO & 0xFF, 0x10 | (n & 0x0F)])
                            + b"\x00\x00\x01\xc0" + n.to_bytes(8, "big") + b"\x55" * 172)
                else:
                    out += bytes([0x47, AUDIO >> 8, AUDIO & 0xFF, 0x10 | (n & 0x0F)]) + b"\x11" * 184
            return bytes(out)
        sp = livetv._ReplaySplicer(lambda: 0.0)
        sp.sent(radio(0, 3000))
        sp.redialled()
        fresh = radio(3517, 4000)                 # an outage longer than the buffer
        out = sp.feed(fresh) + sp.broke()
        self.assertEqual(fresh, out, "new audio was dropped after a real gap")
        self.assertEqual((0, 1), (sp.splices, sp.misses))
        # and a true replay of the same radio stream is still joined exactly
        sp = livetv._ReplaySplicer(lambda: 0.0)
        sp.sent(radio(0, 3000))
        sp.redialled()
        out = sp.feed(radio(2600, 3400)) + sp.broke()
        self.assertEqual(radio(3000, 3400), out)
        self.assertEqual((1, 0), (sp.splices, sp.misses))

    def test_a_looping_stream_is_not_joined(self):
        """The same PES header twice in the last packets sent: a loop, no position."""
        import routers.livetv as livetv
        loop = run(0, 40)
        sp = livetv._ReplaySplicer(lambda: 0.0)
        sp.sent(run(100, 1000) + loop * 5)
        sp.redialled()
        out = sp.feed(loop * 3 + run(2000, 2100)) + sp.broke()
        self.assertEqual(loop * 3 + run(2000, 2100), out)
        self.assertEqual((0, 1), (sp.splices, sp.misses))

    def test_a_header_without_timestamp_recurring_after_a_gap_is_not_joined(self):
        """Fix check of S9b: a PES header without PTS repeats byte for byte; in
        silence with 129-packet PES it recurs every 2,064 packets (past the
        2,048-packet loop check). The bytes before it differ after a real gap."""
        import routers.livetv as livetv

        def hdr(pusi, n):
            return bytes([0x47, (0x40 if pusi else 0) | (AUDIO >> 8), AUDIO & 0xFF, 0x10 | (n & 0x0F)])

        def silent(n):
            if n % 129 == 0:
                return hdr(True, n) + b"\x00\x00\x01\xc0\x00\x00\x80\x00\x00" + b"\x11" * 175
            return hdr(False, n) + b"\x11" * 184

        def speech(n):
            if n % 129 == 0:
                return hdr(True, n) + b"\x00\x00\x01\xc0\x00\x00\x80\x00\x00" + n.to_bytes(8, "big") * 21 + b"\x00" * 7
            return hdr(False, n) + n.to_bytes(8, "big") * 23
        sp = livetv._ReplaySplicer(lambda: 0.0)
        sp.sent(b"".join(silent(n) for n in range(3000)))
        sp.redialled()
        fresh = b"".join(speech(n) for n in range(5000, 5300)) + b"".join(silent(n) for n in range(5300, 7200))
        out = sp.feed(fresh) + sp.broke()
        self.assertEqual(fresh, out, "new audio was dropped after a real gap")
        self.assertEqual((0, 1), (sp.splices, sp.misses))


class SplicedReconnectsAreNotDamage(unittest.IsolatedAsyncioTestCase):
    async def test_a_recording_whose_reconnects_were_all_joined_is_not_reported_damaged(self):
        import routers.livetv as livetv
        h = livetv._new_health()
        h.update(reconnects=5, splices=5, splice_misses=0, reconnecting_seconds=8.0)
        writes = []
        with patch.object(livetv, "log_activity", lambda db, ev, msg, **kw: writes.append(ev)), \
                patch.object(livetv, "SessionLocal", MagicMock):
            livetv._stream_ended(1, {"health": h, "opened_at": asyncio.get_running_loop().time() - 60}, True)
            # nothing is scheduled for a recording that is not damaged; the next
            # one is, and only its entry may arrive
            h = livetv._new_health()
            h.update(reconnects=5, splices=4, splice_misses=1, reconnecting_seconds=8.0)
            livetv._stream_ended(1, {"health": h, "opened_at": asyncio.get_running_loop().time() - 60}, True)
            await until(lambda: writes)
            await asyncio.sleep(0.05)
        self.assertEqual(["livetv_recording_damaged"], writes, "the same recording with one miss is reported")

    async def test_a_reconnect_that_could_not_be_joined_is_reported(self):
        import routers.livetv as livetv
        h = livetv._new_health()
        h.update(reconnects=5, splices=4, splice_misses=1, reconnecting_seconds=40.0)
        with self.assertLogs("routers.livetv", "WARNING") as cm:
            livetv._stream_ended(1, {"health": h, "opened_at": asyncio.get_running_loop().time() - 60}, False)
        self.assertTrue(any("5 interruption" in m for m in cm.output))


class ComposesWithPacketAlignment(unittest.IsolatedAsyncioTestCase):
    """The splice sees what the raw path already made of a connection: a
    re-dial that starts mid-packet has lost its partial packet (#335), and a
    drop mid-packet never sends the cut packet's head."""

    async def test_a_redial_that_starts_mid_packet_inside_the_replay_is_joined(self):
        import routers.livetv as livetv
        livetv._recent_streams.clear()
        body, *_ = await _play(script([run(0, 1000), run(700, 1500)[100:]]))
        self.assertEqual(list(range(1500)), numbers(body))
        last = livetv._recent_streams[-1]
        self.assertEqual((1, 1, 0), (last["reconnects"], last["splices"], last["splice_misses"]))

    async def test_a_drop_mid_packet_then_a_replay_sends_the_cut_packet_once_whole(self):
        body, *_ = await _play(script([run(0, 1000)[:-100], run(700, 1500)]))
        self.assertEqual(list(range(1500)), numbers(body))


class CountersAreTruthful(unittest.IsolatedAsyncioTestCase):
    """Every reconnect is either joined (nothing missing, nothing repeated) or
    counted as a miss; an answer that never delivered the channel is neither."""

    def setUp(self):
        import routers.livetv as livetv
        livetv._recent_streams.clear()

    async def test_each_reconnect_is_joined_or_a_miss_and_the_skipped_bytes_are_the_replay(self):
        import httpx
        import routers.livetv as livetv
        from test_livetv_raw_reconnect import _Body
        page = httpx.Response(200, headers={"content-type": "text/html"},
                              stream=_Body([b"<html>busy</html>"]), request=httpx.Request("GET", TOKENIZED))
        conns = [run(0, 1000), run(700, 1500), run(1400, 2000), run(2600, 2700)]
        s = {PANEL: [_redirect()] * 5 + [_resp(404, PANEL)],
             TOKENIZED: [_live(pieces(conns[0]), then=_dropped()), page,
                         _live(pieces(conns[1]), then=_dropped()),
                         _live(pieces(conns[2]), then=_dropped()),
                         _live(pieces(conns[3]))]}
        body, *_ = await _play(s)
        self.assertEqual(list(range(2000)) + list(range(2600, 2700)), numbers(body))
        last = livetv._recent_streams[-1]
        self.assertEqual((3, 2, 1), (last["reconnects"], last["splices"], last["splice_misses"]),
                         "the error page is not a reconnect; two replays joined, one real gap")
        self.assertEqual(len(run(700, 1000)) + len(run(1400, 1500)), last["replay_bytes_skipped"])

    async def test_a_pingpong_counts_every_joined_reconnect_as_joined(self):
        import routers.livetv as livetv
        conns, live = [run(0, 400)], 400
        for _ in range(8):
            conns.append(run(live - 150, live + 60))
            live += 60
        body, *_ = await _play(script(conns))
        self.assertEqual(list(range(live)), numbers(body))
        last = livetv._recent_streams[-1]
        self.assertEqual((8, 8, 0), (last["reconnects"], last["splices"], last["splice_misses"]))
        self.assertEqual(sum(len(run(a - 150, a)) for a in range(400, live, 60)), last["replay_bytes_skipped"])

    async def test_the_counters_are_current_while_the_stream_runs(self):
        """/api/live/streams shows a running stream's health: whenever bytes go
        out, every reconnect counted so far is already joined or a miss."""
        import routers.livetv as livetv
        from test_livetv_open_single_fetch import FakeClient
        conns, live = [run(0, 2000)], 2000
        for _ in range(3):
            conns.append(run(live - 300, live + 1000))
            live += 1000
        real_sleep = asyncio.sleep

        async def fast_sleep(delay):
            await real_sleep(0)
        seen, out = [], []
        with patch("httpx.AsyncClient", lambda **kw: FakeClient(script(conns), [], [], **kw)), \
                patch("routers.livetv.is_safe_url", lambda *a, **k: True), \
                patch("asyncio.sleep", fast_sleep):
            response = await livetv._stream_proxy_inner(
                channel_id=1, user_agent="T/1", stream_url=PANEL, _release_sem=lambda: None, guard=None)
            async for piece in response.body_iterator:
                out.append(piece)
                h = livetv._stream_status[1]["health"]
                seen.append((h["reconnects"], h["splices"], h["splice_misses"]))
        self.assertEqual(list(range(live)), numbers(b"".join(out)))
        self.assertEqual(3, seen[-1][0])
        self.assertEqual([], [s for s in seen if s[0] != s[1] + s[2]],
                         "a reconnect counted but neither joined nor missed while bytes went out")

    async def test_a_reconnect_still_undecided_when_the_stream_ends_is_reported(self):
        """The stream ended while a replay was held: that reconnect is neither
        joined nor a miss, and the recording must not read as complete."""
        import routers.livetv as livetv
        h = livetv._new_health()
        h.update(reconnects=3, splices=2, splice_misses=0, reconnecting_seconds=3.0)
        writes = []
        with patch.object(livetv, "log_activity", lambda db, ev, msg, **kw: writes.append(ev)), \
                patch.object(livetv, "SessionLocal", MagicMock), \
                self.assertLogs("routers.livetv", "WARNING"):
            livetv._stream_ended(1, {"health": h, "opened_at": asyncio.get_running_loop().time() - 60}, True)
            await until(lambda: writes)
        self.assertIn("livetv_recording_damaged", writes)


class GuardsHold(unittest.TestCase):
    """The rules that keep a join from dropping bytes that were never sent."""

    def test_a_replay_reaching_back_over_an_earlier_gap_is_not_joined(self):
        """After a real gap, a later replay can reach back into the seconds the
        stream never had. Joining it would drop them (the tail, and so the
        context check, is far shorter than a replay at real bitrates), so for
        _SPLICE_AFTER_GAP after a gap nothing is joined."""
        import routers.livetv as livetv
        now = [0.0]
        with patch.object(livetv, "_SPLICE_ANCHOR_MAX", 16):
            sp = livetv._ReplaySplicer(lambda: now[0])
            sp.sent(run(0, 1000))
            now[0] = 10.0
            sp.redialled()                               # back after 1000..1499 were lost
            out = sp.feed(run(1500, 1700)) + sp.broke()
            self.assertEqual((0, 1), (sp.splices, sp.misses))
            sp.sent(out)
            now[0] = 20.0
            sp.redialled()                               # a replay reaching back to 900
            fresh = run(900, 1800)
            out = b"".join(sp.feed(fresh[i:i + 5000]) for i in range(0, len(fresh), 5000)) + sp.broke()
        self.assertEqual([], sorted(set(range(1000, 1500)) - set(numbers(out))),
                         "packets the stream never had were dropped")
        self.assertEqual((0, 2), (sp.splices, sp.misses))

    def test_a_hold_covers_at_most_45_s_of_media_once_the_rate_is_known(self):
        import routers.livetv as livetv
        now = [0.0]
        sp = livetv._ReplaySplicer(lambda: now[0])
        sp.sent(run(0, 100))
        self.assertEqual(min(livetv._SPLICE_MAX_BYTES, livetv._SUBSCRIBER_QUEUE_BYTES // 2), sp._bound(),
                         "before the rate is known, the byte cap bounds a hold")
        sp._bytes, now[0] = 16_000 * 100, 100.0      # 128 kbit/s
        self.assertLessEqual(sp._bound(), 16_000 * livetv._SPLICE_HOLD_SECONDS)
        self.assertLess(livetv._SPLICE_HOLD_SECONDS, livetv._SPLICE_AFTER_GAP)

    def test_released_slices_are_whole_packets(self):
        import routers.livetv as livetv
        self.assertEqual(0, livetv._SPLICE_SLICE % 188)


class ComposesWithClientSlack(unittest.IsolatedAsyncioTestCase):
    """A hold that has to be let go is published in one turn of the event
    loop. A client loses its oldest pieces past _SUBSCRIBER_QUEUE_MAX pieces
    AND _SUBSCRIBER_QUEUE_BYTES (#374), so a released hold must stay well
    inside that slack, or a recording that keeps up would lose content the
    provider delivered."""

    def test_a_hold_is_at_most_half_a_clients_byte_slack(self):
        import routers.livetv as livetv
        now = [0.0]
        sp = livetv._ReplaySplicer(lambda: now[0])
        sp.sent(run(0, 100))
        sp._bytes, now[0] = 10 ** 10, 10.0          # a very fast stream: 1 GB/s
        self.assertLessEqual(sp._bound(), livetv._SUBSCRIBER_QUEUE_BYTES // 2)
        with patch.object(livetv, "_SUBSCRIBER_QUEUE_BYTES", 8 * 1024 * 1024):
            self.assertLessEqual(sp._bound(), 4 * 1024 * 1024)

    async def test_a_released_hold_does_not_cut_into_a_client_that_keeps_up(self):
        import httpx
        import routers.livetv as livetv
        from test_livetv_open_single_fetch import FakeClient
        from test_livetv_raw_reconnect import _Body
        real_sleep = asyncio.sleep

        class Paced(_Body):
            """A provider connection: one read per turn of the event loop."""
            async def __aiter__(self):
                for piece in self._pieces:
                    yield piece
                    await real_sleep(0)
                if self._then is not None:
                    raise self._then

        def conn(data, drop):
            return httpx.Response(200, headers={"content-type": "video/mp2t"},
                                  stream=Paced(pieces(data, size=65536), _dropped() if drop else None),
                                  request=httpx.Request("GET", TOKENIZED))
        # The re-dialled connection replays what was sent, but not up to the
        # drop: the join point never comes, so the replay is held, then let go.
        first, second = run(0, 3000), run(2400, 2990) + run(4000, 25000)
        s = {PANEL: [_redirect(), _redirect(), _resp(404, PANEL)],
             TOKENIZED: [conn(first, True), conn(second, False)]}

        async def fast_sleep(delay):
            await real_sleep(0)

        got = []
        with patch("httpx.AsyncClient", lambda **kw: FakeClient(s, [], [], **kw)), \
                patch("routers.livetv.is_safe_url", lambda *a, **k: True), \
                patch("asyncio.sleep", fast_sleep), \
                patch.object(livetv, "_SUBSCRIBER_QUEUE_MAX", 4), \
                patch.object(livetv, "_SUBSCRIBER_QUEUE_BYTES", 4 * 1024 * 1024), \
                self.assertLogs("routers.livetv", "WARNING") as logs:
            response = await livetv._stream_proxy_inner(
                channel_id=1, user_agent="T/1", stream_url=PANEL, _release_sem=lambda: None, guard=None)
            shared = livetv._SharedUpstream(1, lambda: None)
            q = shared.subscribe()
            pump = asyncio.create_task(shared._pump(response.body_iterator))

            async def client():
                while True:
                    piece = await q.get()
                    if piece is None:
                        return
                    got.append(piece)
                    await real_sleep(0)             # a tuner write: one turn per piece
            await asyncio.wait_for(client(), 60)
            await pump
        self.assertFalse([m for m in logs.output if "dropped a segment" in m],
                         "a client that keeps up lost pieces to a released hold")
        delivered = set(range(3000)) | set(range(4000, 25000))
        self.assertEqual(set(), delivered - set(numbers(b"".join(got))), "content lost")
        self.assertTrue(any("could not be joined" in m for m in logs.output), "the miss was not reported")


class ReplaySpliceProperty(unittest.IsolatedAsyncioTestCase):
    """Random drops, rewinds, outages, channel restarts, tables/nulls or not,
    random read sizes. After every run:
    P1 no loss: every video packet any connection delivered is sent at least once;
    P2 a packet goes out twice only when a re-dial was not joined (and that is
       reported): a joined re-dial never repeats anything;
    P3 whole packets only (numbers() asserts it);
    P4 a real gap (a connection starting after everything delivered so far)
       is always reported as a miss."""

    SEEDS = 1000

    async def test_random_reconnects(self):
        for seed in range(self.SEEDS):
            rng = random.Random(seed)
            psi = rng.random() < 0.7
            audio = rng.random() < 0.7
            conns, live, base = [], 0, 0
            for k in range(rng.randint(1, 6)):
                length = rng.randint(64, 600)
                if k == 0:
                    start = 0
                elif rng.random() < 0.05:            # the provider restarted the channel
                    base = rng.randint(10**6, 10**7)
                    live, start = base, base
                else:
                    live += rng.randint(0, 60) if rng.random() < 0.9 else rng.randint(200, 800)
                    start = max(base, live - rng.randint(0, 250))
                conns.append((start, live + length))
                live += length
            import routers.livetv as livetv
            reported = []
            real_gap = any(a > max(bb for _, bb in conns[:i]) for i, (a, _) in enumerate(conns) if i)
            orig = livetv._ReplaySplicer._gap

            def gap(self, why, _orig=orig):
                reported.append(why)
                _orig(self, why)
            with patch.object(livetv._ReplaySplicer, "_gap", gap):
                body, *_ = await _play(script([run(a, b, psi, audio) for a, b in conns], rng))
            got = numbers(body)
            # P4 (review F11): a real gap is always reported
            if real_gap:
                self.assertTrue(reported, f"seed {seed}: a real gap was not reported")
            delivered = set().union(*[set(range(a, b)) for a, b in conns])
            self.assertEqual(set(), delivered - set(got), f"seed {seed}: content lost")
            # P2: repeats happen only when a re-dial was not joined (a reported miss)
            if not reported:
                self.assertEqual(len(got), len(set(got)), f"seed {seed}: repeats without a reported miss")


if __name__ == "__main__":
    unittest.main()
