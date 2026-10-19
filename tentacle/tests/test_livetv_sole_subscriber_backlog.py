"""A client of a shared Live TV upstream gets every byte of a backlog it can catch up with.

Run from the tentacle/ directory:  python -m unittest discover -s tests -p "test_livetv_sole_subscriber_*.py"

The shared upstream (one provider connection per channel, fanned out) hands each
client its pieces through a queue, and a client too far behind loses its oldest
piece so it cannot stall the upstream or the other clients. The slack was 32
PIECES. For HLS a piece is a whole segment (minutes of slack), but the raw TS
path sends ~128 KB pieces, so 32 of them is a few seconds of an HD channel --
less than the backlog the provider hands over after any pause (a stalled event
loop, a frozen container, the replay a panel sends on every re-dial). The pump
then ran ahead of the client and pieces were dropped even when the client was a
recording and the only one on the channel: programme content cut out for good.
"""
import asyncio
import unittest
from unittest import mock

from fastapi.responses import StreamingResponse

import test_livetv_stream_sharing as _sharing

PIECE = 128 * 1024      # what the raw TS path publishes
CLIENT_TURNS = 3        # loop turns a tuner write takes per piece (a socket drain)


def _piece(n: int) -> bytes:
    return n.to_bytes(4, "big") + b"\x47" * (PIECE - 4)


class SubscriberBacklog(unittest.TestCase):
    setUp = _sharing.SharedUpstream.setUp
    _channel = _sharing.SharedUpstream._channel

    def _drive(self, pieces, readers, pump_turns=1):
        """Open one channel with a provider backlog of `pieces`, read it with
        `readers` (each: turns per piece, or None for a client that never reads
        after the first piece). Returns ([indices received per reader], logs)."""
        livetv = self.livetv
        cid = self._channel()

        async def fake_inner(channel_id, ua, url, release, guard=None, **kw):
            async def gen():
                try:
                    for n in range(pieces):
                        yield _piece(n)
                        for _ in range(pump_turns):
                            await asyncio.sleep(0)      # a socket read with data ready
                finally:
                    release()
            return StreamingResponse(gen(), media_type="video/mp2t")

        async def read(resp, turns):
            got = []
            while True:
                try:
                    piece = await asyncio.wait_for(resp.body_iterator.__anext__(), 10)
                except StopAsyncIteration:
                    return got
                got.append(int.from_bytes(piece[:4], "big"))
                if turns is None:
                    await asyncio.sleep(3600)           # stuck: never reads again
                for _ in range(turns):
                    await asyncio.sleep(0)

        async def drive():
            with mock.patch.object(livetv, "_stream_proxy_inner", fake_inner), \
                    mock.patch.object(livetv, "is_safe_url", lambda *a, **k: True), \
                    mock.patch.object(livetv, "lan_origin_guard", lambda *a, **k: (lambda url: True)):
                resps = [await livetv.stream_proxy(cid, self.db) for _ in readers]
                tasks = [asyncio.create_task(read(r, t)) for r, t in zip(resps, readers)]
                live = [t for t, turns in zip(tasks, readers) if turns is not None]
                results = await asyncio.wait_for(asyncio.gather(*live), 30)
                for t in tasks:
                    t.cancel()
                return results, resps

        with self.assertLogs("routers.livetv", level="INFO") as logs:
            results, resps = asyncio.run(drive())
        return results, logs.output, resps

    def test_the_only_client_loses_nothing_of_a_backlog(self):
        pieces = 200                                    # 25 MB: ~25 s of an 8 Mbit/s channel
        (got,), logs, _ = self._drive(pieces, [CLIENT_TURNS])
        dropped = sum("dropped a segment" in line for line in logs)
        self.assertEqual(
            got, list(range(pieces)),
            f"the only client on the channel (a recording) received {len(got)} of {pieces} pieces: "
            f"{pieces - len(got)} were dropped ({dropped} 'is behind' warnings), although no other "
            f"client was waiting on it")

    def test_a_stuck_client_does_not_hold_up_the_others_and_its_backlog_is_bounded(self):
        """What the drop is for still holds: a client that stops reading never
        stalls the upstream or its peers, and what it holds stays bounded."""
        livetv = self.livetv
        pieces = 200
        slack = 8 * 1024 * 1024                         # a small slack keeps the test light
        with mock.patch.object(livetv, "_SUBSCRIBER_QUEUE_BYTES", slack, create=True):
            (got,), logs, resps = self._drive(pieces, [0, None], pump_turns=10)
        self.assertEqual(got, list(range(pieces)), "a stuck client held up the client that reads")
        stuck_q = resps[1]._q
        self.assertLessEqual(stuck_q.qsize(), max(livetv._SUBSCRIBER_QUEUE_MAX, slack // PIECE + 1) + 1,
                             "the stuck client's backlog was not bounded")
        self.assertTrue(any("dropped a segment" in line for line in logs),
                        "the stuck client was never dropped: its backlog grew without bound")


if __name__ == "__main__":
    unittest.main()
