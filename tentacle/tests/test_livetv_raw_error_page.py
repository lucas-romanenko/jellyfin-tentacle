"""A provider error page served with HTTP 200 is never proxied as the channel (#299).

Run from the tentacle/ directory:  python -m unittest discover -s tests

Some Xtream panels answer a stream request with 200 OK and an error body
(`{"error":"There is an Database Error","status":false}`, an HTML page). The
raw-TS path passed that body to the tuner as video: a Jellyfin recording of 53
bytes of JSON at open, or an HTML page spliced into a running recording at a
re-dial. The first bytes of every raw connection are now checked: MPEG-TS
starts with the sync byte 0x47, and a non-media type or a body that starts
like text is waited out like a refusal.
"""
import unittest

import httpx

from test_livetv_open_single_fetch import PANEL, TOKENIZED, _redirect, _resp
from test_livetv_raw_reconnect import _Body, _dropped, _live
from test_livetv_reconnect_budget import _play

A = b"G" + b"a" * 187          # one TS packet
B = b"G" + b"b" * 187
ERR = b'{"error":"There is an Database Error","status":false}'
HTML = b"<html><head><title>502 Bad Gateway</title></head><body>" + b"x" * 900 + b"</body></html>"


def _page(body, ct, url=TOKENIZED):
    headers = {"content-type": ct} if ct else {}
    return httpx.Response(200, headers=headers, stream=_Body([body]),
                          request=httpx.Request("GET", url))


class ErrorPageAtOpen(unittest.IsolatedAsyncioTestCase):
    async def _open(self, page):
        script = {PANEL: [_redirect(), _redirect(), _resp(404, PANEL)],
                  TOKENIZED: [page, _live([B])]}
        try:
            body, *_ = await _play(script, failure_budget=120)
        except Exception:
            return b""                      # refused at open: fine
        return body

    async def test_a_json_error_page_labelled_text_html_is_not_proxied(self):
        body = await self._open(_page(ERR, "text/html; charset=UTF-8"))
        self.assertNotIn(ERR, body)
        self.assertEqual(B, body)           # the retry got the channel

    async def test_an_html_error_page_without_content_type_is_not_proxied(self):
        self.assertNotIn(b"Bad Gateway", await self._open(_page(HTML, None)))

    async def test_the_tuner_is_told_mpeg_ts_not_the_error_page_type(self):
        import routers.livetv as livetv
        self.assertEqual("video/mp2t", livetv._raw_media_type("text/html; charset=UTF-8"))
        self.assertEqual("video/mp2t", livetv._raw_media_type("application/json"))
        self.assertEqual("video/mp2t", livetv._raw_media_type(""))
        self.assertEqual("video/mp2t", livetv._raw_media_type("video/mp2t"))
        self.assertEqual("application/octet-stream", livetv._raw_media_type("application/octet-stream"))


class ErrorPageAtRedial(unittest.IsolatedAsyncioTestCase):
    async def test_a_1kb_error_page_while_redialling_is_not_spliced_into_the_recording(self):
        script = {PANEL: [_redirect()] * 3 + [_resp(404, PANEL)],
                  TOKENIZED: [_live([A], then=_dropped()), _page(HTML, "text/html"), _live([B])]}
        body, *_ = await _play(script, failure_budget=120)
        self.assertEqual(A + B, body)

    async def test_a_channel_that_only_answers_the_error_page_ends_within_a_viewers_budget(self):
        # A fresh page per request (a streamed body can be read only once).
        script = {PANEL: [_redirect()], TOKENIZED: [_page(ERR, "application/json") for _ in range(200)]}
        body, log, slept = await _play(script, failure_budget=60, is_recording=lambda: False)
        self.assertEqual(b"", body)
        self.assertLessEqual(sum(slept), 60 + 18.1)


    async def test_a_slow_error_page_does_not_reset_a_viewers_budget(self):
        """A page whose first byte comes after HEALTHY_AFTER is still a failure,
        not a recovery: the budget runs out."""
        import asyncio
        from unittest.mock import patch

        clock = {"t": 0.0}

        class _Slow(_Body):
            async def __aiter__(self):
                clock["t"] += 11.0
                async for piece in super().__aiter__():
                    yield piece
        pages = [httpx.Response(200, headers={"content-type": "text/html"}, stream=_Slow([ERR]),
                                request=httpx.Request("GET", TOKENIZED)) for _ in range(60)]
        script = {PANEL: [_redirect()] * 50 + [_resp(404, PANEL)],
                  TOKENIZED: [_live([A], then=_dropped())] + pages}
        loop = asyncio.get_running_loop()
        real_time = loop.time
        loop.slow_callback_duration = 3600      # the clock jumps on purpose
        with patch.object(loop, "time", lambda: real_time() + clock["t"]):
            body, log, slept = await _play(script, failure_budget=60, is_recording=lambda: False)
        self.assertEqual(A, body)
        self.assertLess(log.count(PANEL), 30, "the budget never ran out")

    async def _clocked(self, script, clock, **kw):
        import asyncio
        from unittest.mock import patch
        loop = asyncio.get_running_loop()
        real_time = loop.time
        loop.slow_callback_duration = 3600      # the clock jumps on purpose
        with patch.object(loop, "time", lambda: real_time() + clock["t"]):
            return await _play(script, **kw)

    async def test_one_packet_then_silence_until_the_close_does_not_reset_the_budget(self):
        """A panel whose source is offline sends a packet, holds the connection
        and closes it: delivered, but not past HEALTHY_AFTER, so not a recovery."""
        clock = {"t": 0.0}

        class _OneThenHang(_Body):
            async def __aiter__(self):
                yield A
                clock["t"] += 11.0
        conns = [httpx.Response(200, headers={"content-type": "video/mp2t"}, stream=_OneThenHang([]),
                                request=httpx.Request("GET", TOKENIZED)) for _ in range(60)]
        script = {PANEL: [_redirect()] * 60 + [_resp(404, PANEL)],
                  TOKENIZED: [_live([A], then=_dropped())] + conns}
        body, log, slept = await self._clocked(script, clock, failure_budget=60, is_recording=lambda: False)
        self.assertLess(log.count(PANEL), 30, "the budget never ran out")

    async def test_a_late_start_that_keeps_delivering_is_still_a_recovery(self):
        clock = {"t": 0.0}

        class _Late(_Body):
            async def __aiter__(self):
                clock["t"] += 11.0
                yield A
                clock["t"] += 5.0
                yield B
                raise _dropped()
        conns = [httpx.Response(200, headers={"content-type": "video/mp2t"}, stream=_Late([]),
                                request=httpx.Request("GET", TOKENIZED)) for _ in range(4)]
        script = {PANEL: [_redirect()] * 5 + [_resp(404, PANEL)],
                  TOKENIZED: [_live([A], then=_dropped())] + conns}
        body, log, slept = await self._clocked(script, clock, failure_budget=60, is_recording=lambda: False)
        self.assertEqual(A + (A + B) * 4, body)


class NoFalsePositive(unittest.IsolatedAsyncioTestCase):
    async def test_a_stream_starting_mid_packet_is_accepted(self):
        """A connection that starts mid-packet can begin with "<" or "{"; the
        sync byte every 188 bytes after it says it is MPEG-TS, whatever the label."""
        for lead in (b"<", b"{"):
            for ct in ("video/mp2t", None, "text/html"):
                data = lead + b"z" * 99 + A + B + A
                script = {PANEL: [_redirect(), _resp(404, PANEL)], TOKENIZED: [_page(data, ct)]}
                body, *_ = await _play(script, failure_budget=120)
                self.assertEqual(data, body, (lead, ct))

    async def test_a_mid_packet_start_in_small_pieces_is_accepted(self):
        """The first read can be a few bytes: the start is judged on enough of it."""
        data = b"<" + b"z" * 99 + A + B + A
        pieces = [data[:100], data[100:150]] + [data[i:i + 64] for i in range(150, len(data), 64)]
        script = {PANEL: [_redirect(), _resp(404, PANEL)],
                  TOKENIZED: [httpx.Response(200, headers={"content-type": "video/mp2t"}, stream=_Body(pieces),
                                             request=httpx.Request("GET", TOKENIZED))]}
        body, *_ = await _play(script, failure_budget=120)
        self.assertEqual(data, body)

    async def test_an_error_page_labelled_as_video_is_still_refused(self):
        for page in (ERR, HTML):
            script = {PANEL: [_redirect(), _redirect(), _resp(404, PANEL)],
                      TOKENIZED: [_page(page, "video/mp2t"), _live([B])]}
            body, *_ = await _play(script, failure_budget=120)
            self.assertEqual(B, body)

    async def test_real_ts_with_a_generic_or_wrong_label_is_accepted(self):
        for ct in ("application/octet-stream", "text/plain", "text/html", None):
            script = {PANEL: [_redirect(), _resp(404, PANEL)], TOKENIZED: [_page(A + B, ct)]}
            body, *_ = await _play(script, failure_budget=120)
            self.assertEqual(A + B, body, ct)

    async def test_binary_that_is_not_ts_and_not_text_is_still_proxied(self):
        # Some panels serve other containers; only text-looking bodies or a
        # text type are taken for an error page.
        blob = b"\x00\x00\x01\xba" + b"\x11" * 400
        script = {PANEL: [_redirect(), _resp(404, PANEL)],
                  TOKENIZED: [_page(blob, "application/octet-stream")]}
        body, *_ = await _play(script, failure_budget=120)
        self.assertEqual(blob, body)


if __name__ == "__main__":
    unittest.main()
