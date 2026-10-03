"""A re-dialled raw MPEG-TS connection must not write the provider's replay twice.

Run from the tentacle/ directory:  python -m unittest discover -s tests -p "test_livetv_raw_replay_*.py"

Many Xtream panels start every (re)opened raw TS connection with their buffer:
the last ~20 s of the channel, byte for byte the packets an earlier connection
already delivered. stream_generator re-dials after a drop (#103, #298) and
forwards every byte of the new connection, so those packets reach the tuner a
second time. On an account that closes the older of two connections every
~13 s, every recording repeats ~22 s after each reconnect.
"""
import unittest
from collections import Counter

from test_livetv_open_single_fetch import PANEL, TOKENIZED, _redirect, _resp
from test_livetv_raw_reconnect import _dropped, _live, _play


def pkt(n: int) -> bytes:
    """TS packet n of the channel: PID 0x100, each one the start of a PES
    (00 00 01 e0 first, as at every video frame), payload unique per packet."""
    head = bytes([0x47, 0x41, 0x00, 0x10 | (n & 0x0F)])
    body = b"\x00\x00\x01\xe0" + n.to_bytes(8, "big")
    return head + body + b"\xff" * (188 - len(head) - len(body))


def packets(data: bytes) -> list:
    return [int.from_bytes(data[i + 8:i + 16], "big") for i in range(0, len(data) - 187, 188)]


def connection(first: int, last: int, then_drop=True):
    """One provider connection delivering packets first..last-1 (in a few pieces)."""
    data = b"".join(pkt(n) for n in range(first, last))
    pieces = [data[i:i + 188 * 7] for i in range(0, len(data), 188 * 7)]
    return _live(pieces, then=_dropped() if then_drop else None)


def pingpong(reconnects: int, replay: int, fresh: int):
    """Connection k delivers `replay` packets already sent, then `fresh` new ones, then drops."""
    conns, pos = [], 0
    for k in range(reconnects + 1):
        start = max(0, pos - replay) if k else 0
        conns.append(connection(start, pos + fresh, then_drop=k < reconnects))
        pos += fresh
    script = {PANEL: [_redirect()] * (reconnects + 1) + [_resp(404, PANEL)],
              TOKENIZED: conns}
    return script, pos


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


if __name__ == "__main__":
    unittest.main()
