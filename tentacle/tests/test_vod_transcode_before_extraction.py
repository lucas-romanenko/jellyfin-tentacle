"""A TV app's film is transcoded by Jellyfin, so the player never reads the
file itself: Jellyfin's ffmpeg for the playback and its subtitle extraction
both come from Jellyfin's address, and neither is "the viewer". In equal turns
on the one provider connection the extraction (which reads the whole file)
took it from the transcode every 8 s, and the TV stopped 5-9 s into the film,
every first play (production, 2026-11-05). The plugin's extraction now reads
with WORK_USER_AGENT, and the playback's other requests come first.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import asyncio
import re
import unittest
from pathlib import Path

from unittest import mock

from starlette.requests import Request

from test_vod_viewers import _PacedRangeFake, _ViewersBase

MARK = "Tentacle-JellyfinWork"     # the plugin's WorkUserAgent and routers.vod.WORK_USER_AGENT

PATCH = Path("../tentacle-plugin/Services/VodSubtitleExtractionPatch.cs")


def _request(range_header=None, client="172.19.0.1", user_agent=None):
    headers = [(b"host", b"tentacle")]
    if range_header:
        headers.append((b"range", range_header.encode()))
    if user_agent:
        headers.append((b"user-agent", user_agent.encode()))

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}
    return Request({"type": "http", "method": "GET", "headers": headers, "path": "/", "query_string": b"",
                    "scheme": "http", "server": ("tentacle", 8888), "client": (client, 1)}, receive)


class TheTranscodeComesBeforeTheMarkedExtraction(_ViewersBase):
    DATA = bytes(range(256)) * 16      # 4 KB: both readers span many quanta

    def _run(self, scenario):
        fake = _PacedRangeFake(self.DATA, self.log)
        self.fake = fake

        async def go():
            with mock.patch("httpx.AsyncClient", fake), \
                    mock.patch("routers.vod.lan_origin_guard", lambda *a, **k: (lambda u: True)), \
                    mock.patch.object(self.vod, "QUANTUM_SECONDS", 0.02), \
                    mock.patch.object(self.vod, "PREEMPT_GRACE_SECONDS", 0.02):
                return await scenario()
        return asyncio.run(go())

    async def _read(self, resp):
        return b"".join([c async for c in resp.body_iterator])

    async def _open_as(self, range_header="bytes=0-", user_agent=None):
        return await self.vod.vod_stream("movie", self.token_file,
                                         _request(range_header, user_agent=user_agent), self.db)

    def test_a_reading_transcode_is_never_parked_for_the_marked_extraction(self):
        self.watching = {self.title: ["192.0.2.99"]}      # the TV's address: it reads nothing itself

        async def scenario():
            transcode = await self._open_as("bytes=0-4094", user_agent="Lavf/61.7.100")
            it = transcode.body_iterator.__aiter__()
            first = await it.__anext__()
            self.assertTrue(await self._until(lambda: self.vod._viewers["titles"] is not None))
            extraction = await self._open_as(user_agent=MARK)
            ex = asyncio.ensure_future(self._read(extraction))
            rest = b""
            async for c in it:                          # a transcode's pace: many quanta long
                rest += c
                await asyncio.sleep(0.002)
            return first + rest, await ex
        a, b = self._run(scenario)
        self.assertEqual(self.DATA[:4095], a)
        self.assertEqual(self.DATA, b, "the extraction still gets the whole file")
        resumed = [rng for _, rng, _ in self.log if rng and rng.endswith("-4094") and rng != "bytes=0-4094"]
        # the extraction's own open parks it once; it takes the connection back at the extraction's first chunk
        self.assertLessEqual(len(resumed), 1, resumed)
        self.assertEqual(1, self.fake.most_open)

    def test_the_marked_extraction_reads_while_the_transcode_does_not(self):
        self.watching = {self.title: ["192.0.2.99"]}

        async def scenario():
            transcode = await self._open_as(user_agent="Lavf/61.7.100")
            it = transcode.body_iterator.__aiter__()
            head = await it.__anext__()
            self.assertTrue(await self._until(lambda: self.vod._viewers["titles"] is not None))
            extraction = await self._open_as(user_agent=MARK)
            done = await asyncio.wait_for(asyncio.ensure_future(self._read(extraction)), 5)
            rest = b"".join([c async for c in it])
            return head + rest, done
        a, b = self._run(scenario)
        self.assertEqual(self.DATA, a, "the transcode ended early or skipped bytes")
        self.assertEqual(self.DATA, b, "the extraction did not get the idle connection")
        self.assertEqual(1, self.fake.most_open)

    def test_unmarked_requests_still_share_in_equal_turns(self):
        """An older plugin marks nothing: as before (test_vod_viewers)."""
        self.watching = {self.title: ["192.0.2.99"]}

        async def scenario():
            a = await self._open_as(user_agent="Lavf/61.7.100")
            b = await self._open_as(user_agent="Lavf/61.7.100")
            return await asyncio.gather(self._read(a), self._read(b))
        a, b = self._run(scenario)
        self.assertEqual((self.DATA, self.DATA), (a, b))
        self.assertGreater(len(self.log), 2, "they took turns")
        self.assertEqual(1, self.fake.most_open)


class ThePluginMarksItsExtraction(unittest.TestCase):
    def setUp(self):
        self.src = PATCH.read_text(encoding="utf-8")

    def test_the_marker_is_the_same_on_both_sides(self):
        import routers.vod as vod
        m = re.search(r'public const string WorkUserAgent = "([^"]+)";', self.src)
        self.assertIsNotNone(m)
        self.assertEqual(MARK, m.group(1))
        self.assertEqual(MARK, vod.WORK_USER_AGENT)

    def test_the_extraction_runs_with_it_as_an_input_option(self):
        self.assertIn('"-user_agent \\"" + WorkUserAgent + "\\" " + args', self.src)
        prefix = self.src[self.src.index("private static bool Prefix("):]
        prefix = prefix[:prefix.index("private static async Task ExtractWhole(")]
        self.assertIn("partialArgs = WithWorkUserAgent(partialArgs);", prefix)
        self.assertLess(prefix.index("WithWorkUserAgent(partialArgs)"), prefix.index("ExtractWhole("))


if __name__ == "__main__":
    unittest.main()
