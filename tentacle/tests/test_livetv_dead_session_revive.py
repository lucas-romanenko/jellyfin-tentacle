"""#184: a dead HLS session refused with 509 is re-resolved -- unless a rival
on the same account is delivering.

Seen in production (755ea67 backend, m3u8, an account where the newest
connection wins): a 230-minute recording got 509 on every playlist refresh
from the moment a second recording opened another channel. Since #136 a
recording never gives up, so the worker asked the SAME tokenized URL every
15 s for 7,481 s; about 2 h 05 min of the game were lost, and Jellyfin showed
the recording as completed. The session had been ended for good.

The rule: a running HLS stream refused (429/509) for _REVIVE_AFTER s in a row
resolves the channel URL again in the same response, only when no rival is
delivering (another pull on the same provider account, of equal or higher
priority, delivered within _RIVAL_FRESH s). The cooldown between attempts
doubles from _REVIVE_COOLDOWN to _REVIVE_COOLDOWN_CAP and resets once
segments flow. Without the `rival_delivering` callable nothing re-resolves on
a refusal (today's behaviour). Raw TS is untouched.

Times are scaled down (the module constants are patched) and run on the real
clock, so the worker's own timing logic is what is tested.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import asyncio
import unittest
from unittest.mock import MagicMock, patch

import httpx

import routers.livetv as livetv

CHANNEL = "http://p.test/live/u/p/7.m3u8"
PL = {"content-type": "application/vnd.apple.mpegurl"}
TS = {"content-type": "video/mp2t"}

AFTER, COOLDOWN, CAP = 0.4, 0.5, 2.0
_REAL_SLEEP = asyncio.sleep   # _run patches asyncio.sleep; side tasks keep real time


def _r(status, url, content=b"", headers=None):
    return httpx.Response(status, headers=headers or {}, content=content,
                          request=httpx.Request("GET", url))


class NewestWins:
    """A panel where the channel URL hands out a session (302 to a
    tokenized URL) and a new session ends every older one for good: 509 on
    its playlist and segments. `kill()` is someone else opening the account.
    `channel_status` makes the channel URL itself answer that status."""

    def __init__(self):
        self.alive = set()
        self.issued = 0
        self.seq = 0
        self.log = []
        self.resolves = []          # loop times of channel URL requests
        self.channel_status = None
        self.dead_on_arrival = False
        self.expire = set()         # tokens whose next playlist read answers 407
        self.kill_first = False     # the first session is ended while it opens (Q5)

    def kill(self):
        self.alive.clear()

    def route(self, url):
        self.log.append(url)
        if url == CHANNEL:
            self.resolves.append(asyncio.get_running_loop().time())
            if self.channel_status:
                return _r(self.channel_status, url)
            self.issued += 1
            tok = f"T{self.issued}"
            self.alive = set() if self.dead_on_arrival else {tok}
            if self.kill_first and self.issued == 1:
                self.alive = set()
            return _r(302, url, headers={"location": f"http://edge.test/{tok}/index.m3u8"})
        tok = url.split("/")[3]
        if url.endswith("index.m3u8"):
            if tok in self.expire:
                self.expire.discard(tok)
                self.alive.discard(tok)
                return _r(407, url)
            if tok not in self.alive:
                return _r(509, url)
            self.seq += 1
            first = max(0, self.seq - 2)
            lines = ["#EXTM3U", "#EXT-X-TARGETDURATION:2", f"#EXT-X-MEDIA-SEQUENCE:{first}"]
            for n in range(first, self.seq + 1):
                lines += ["#EXTINF:2.0,", f"{n}.ts"]
            return _r(200, url, ("\n".join(lines) + "\n").encode(), PL)
        if url.endswith(".ts"):
            if tok not in self.alive:
                return _r(509, url)
            n = int(url.rsplit("/", 1)[1][:-3])
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
        await asyncio.sleep(0)
        return self.panel.route(str(request.url))


def _scaled():
    return [patch.object(livetv, "_REVIVE_AFTER", AFTER), patch.object(livetv, "_REVIVE_COOLDOWN", COOLDOWN),
            patch.object(livetv, "_REVIVE_COOLDOWN_CAP", CAP)]


async def _run(panel, script, rival=None, recording=True, budget=120.0, seconds=6.0, pass_rival=True):
    """Stream channel 7 for `seconds` of real time. `script(got, panel)` is
    called after each segment (to kill the session at some point). Returns
    (segment numbers received, ended early?, the stream summary)."""
    async def quick_sleep(d):
        await _REAL_SLEEP(min(d, 0.03))

    livetv._recent_streams.clear()
    kwargs = {"rival_delivering": rival} if pass_rival else {}
    patches = _scaled() + [patch("httpx.AsyncClient", lambda **kw: _Client(panel)),
                           patch("routers.livetv.is_safe_url", lambda *a, **k: True),
                           patch("asyncio.sleep", quick_sleep),
                           patch.object(livetv, "log_activity", lambda *a, **k: None),
                           patch.object(livetv, "SessionLocal", MagicMock())]
    for p in patches:
        p.start()
    try:
        resp = await livetv._stream_proxy_inner(
            channel_id=7, user_agent="UA", stream_url=CHANNEL, _release_sem=lambda: None,
            failure_budget=budget, is_recording=lambda: recording, **kwargs)
        got = []

        async def collect():
            async for piece in resp.body_iterator:
                for i in range(0, len(piece), 188):
                    got.append(int(piece[i + 1:i + 5]))
                    script(got, panel)
        ended = False
        try:
            await asyncio.wait_for(collect(), timeout=seconds)
            ended = True
        except asyncio.TimeoutError:
            pass
        await resp.body_iterator.aclose()
    finally:
        for p in reversed(patches):
            p.stop()
    return got, ended, (livetv._recent_streams[-1] if livetv._recent_streams else None)


def _kill_at(n):
    def script(got, panel):
        if len(got) == n:
            panel.kill()
    return script


class Revive(unittest.IsolatedAsyncioTestCase):
    async def test_a_dead_session_with_no_rival_is_re_resolved_and_the_recording_goes_on(self):
        panel = NewestWins()
        got, ended, last = await _run(panel, _kill_at(4), rival=lambda: None, seconds=4.0)
        self.assertFalse(ended)
        self.assertEqual(2, len(panel.resolves), "one open, one re-resolve")
        self.assertGreaterEqual(panel.resolves[1] - panel.resolves[0], AFTER,
                                "re-resolved before the refusals had lasted _REVIVE_AFTER")
        self.assertGreater(len(got), 6, f"the recording did not go on after the re-resolve: {got}")
        self.assertEqual(len(got), len(set(got)), f"a segment was sent twice: {got}")
        self.assertEqual(1, last["revives"], last)
        self.assertGreaterEqual(last["reconnects"], 1, last)

    async def test_the_cooldown_doubles_between_attempts(self):
        panel = NewestWins()

        def script(got, p):
            if len(got) == 3:
                p.kill()
                p.dead_on_arrival = True     # every fresh session is refused too
        await _run(panel, script, rival=lambda: None, seconds=5.5)
        gaps = [b - a for a, b in zip(panel.resolves[1:], panel.resolves[2:])]
        self.assertGreaterEqual(len(gaps), 2, panel.resolves)
        self.assertGreaterEqual(gaps[0], COOLDOWN * 0.95, gaps)
        self.assertGreaterEqual(gaps[1], COOLDOWN * 2 * 0.95, gaps)
        self.assertLessEqual(len(panel.resolves), 1 + 5, "the account must not be hammered")

    async def test_a_delivering_rival_holds_it_and_its_end_releases_it(self):
        panel = NewestWins()
        state = {"rival": object(), "released_at": None}

        def rival():
            return state["rival"]
        rival_obj = MagicMock(kind="recording", owner="channel:8")
        state["rival"] = rival_obj

        async def release_later():
            await _REAL_SLEEP(1.5)
            state["rival"] = None
            state["released_at"] = asyncio.get_running_loop().time()
        task = asyncio.create_task(release_later())
        got, ended, last = await _run(panel, _kill_at(3), rival=rival, seconds=4.0)
        await task
        self.assertEqual(2, len(panel.resolves), panel.resolves)
        self.assertGreaterEqual(panel.resolves[1], state["released_at"],
                                "it took the account while the rival was delivering")
        self.assertGreater(len(got), 4, got)

    async def test_it_waits_for_the_whole_timer_while_the_rival_delivers_and_reports_the_hole(self):
        """#137: a recording that ends while still refused is damaged."""
        panel = NewestWins()
        rival_obj = MagicMock(kind="recording", owner="channel:8")
        got, ended, last = await _run(panel, _kill_at(3), rival=lambda: rival_obj, seconds=2.0)
        self.assertEqual(1, len(panel.resolves), "never re-resolve against a delivering rival")
        self.assertTrue(last["ended_on_error"], last)
        self.assertGreater(last["reconnecting_seconds"], AFTER, last)
        self.assertEqual(0, last["revives"], last)

    async def test_without_the_callable_nothing_is_re_resolved(self):
        panel = NewestWins()
        got, ended, last = await _run(panel, _kill_at(3), pass_rival=False, seconds=2.0)
        self.assertEqual(1, len(panel.resolves), "755ea67 behaviour: never re-resolve on a refusal")
        self.assertEqual(3, len(got))

    async def test_a_viewer_whose_budget_runs_out_ends_as_before(self):
        panel = NewestWins()
        rival_obj = MagicMock(kind="recording", owner="channel:8")
        got, ended, last = await _run(panel, _kill_at(3), rival=lambda: rival_obj, recording=False,
                                      budget=1.0, seconds=4.0)
        self.assertTrue(ended, "a viewer against a delivering recording ends after its budget")
        self.assertEqual(1, len(panel.resolves))

    async def test_refusals_interrupted_by_segments_do_not_add_up(self):
        """Only an UNBROKEN run of refusals counts: a session that delivers
        between refusals is alive."""
        panel = NewestWins()
        real = panel.route
        state = {"n": 0}

        def flaky(url):
            if url.endswith("index.m3u8") and panel.seq > 3:
                state["n"] += 1
                if state["n"] % 3:
                    return _r(509, url)
            return real(url)
        panel.route = flaky
        await _run(panel, lambda g, p: None, rival=lambda: None, seconds=2.5)
        self.assertEqual(1, len(panel.resolves), panel.resolves)


class ReResolveCounters(unittest.IsolatedAsyncioTestCase):
    """The 407 token re-resolve (73d623e / 34be500) keeps its own counter."""

    async def test_revives_do_not_count_toward_max_reresolve(self):
        """Refused fresh sessions, re-resolved again and again with no
        segment in between, must not end a recording as 'still 407 after
        _MAX_RERESOLVE resolves'."""
        panel = NewestWins()

        def script(got, p):
            if len(got) == 3:
                p.kill()
                p.dead_on_arrival = True
        got, ended, last = await _run(panel, script, rival=lambda: None, seconds=5.5)
        self.assertFalse(ended, "a recording ended on refused re-resolves")
        self.assertGreater(len(panel.resolves) - 1, livetv._MAX_RERESOLVE - 1, panel.resolves)

    async def test_a_token_expiry_after_a_revive_is_still_re_resolved(self):
        panel = NewestWins()

        def script(got, p):
            if len(got) == 3:
                p.kill()
            if len(got) == 8:
                p.expire.add(f"T{p.issued}")
        got, ended, last = await _run(panel, script, rival=lambda: None, seconds=4.0)
        self.assertFalse(ended)
        self.assertEqual(3, len(panel.resolves), "open, revive, token re-resolve")
        self.assertEqual(1, last["revives"], last)
        self.assertEqual(len(got), len(set(got)), f"a segment was sent twice: {got}")

    async def test_a_refused_channel_url_does_not_end_a_recording(self):
        panel = NewestWins()

        def script(got, p):
            if len(got) == 3:
                p.kill()
                p.channel_status = 509
        got, ended, last = await _run(panel, script, rival=lambda: None, seconds=3.0)
        self.assertFalse(ended)
        self.assertGreaterEqual(len(panel.resolves), 2)


class RivalTest(unittest.IsolatedAsyncioTestCase):
    """_rival_delivering: same account, equal or higher priority, fresh."""

    def setUp(self):
        self.saved = dict(livetv._stream_slots.leases)
        livetv._stream_slots.leases.clear()
        self.saved_status = dict(livetv._stream_status)
        livetv._stream_status.clear()

    def tearDown(self):
        livetv._stream_slots.leases.clear()
        livetv._stream_slots.leases.update(self.saved)
        livetv._stream_status.clear()
        livetv._stream_status.update(self.saved_status)

    def _lease(self, kind, channel_id, provider_id, delivered_ago=None):
        lease = livetv._stream_slots._grant(kind, f"channel:{channel_id}")
        lease.provider_id, lease.channel_id = provider_id, channel_id
        if delivered_ago is not None:
            now = asyncio.get_running_loop().time()
            livetv._stream_status[channel_id] = {"state": "streaming", "since": now, "opened_at": now,
                                                 "last_error": None, "last_ok": now - delivered_ago}
        return lease

    async def test_a_delivering_recording_on_the_same_account_is_a_rival(self):
        a = self._lease("recording", 1, 5)
        b = self._lease("recording", 2, 5, delivered_ago=2)
        self.assertIs(b, livetv._rival_delivering(a))

    async def test_another_account_is_not(self):
        a = self._lease("recording", 1, 5)
        self._lease("recording", 2, 6, delivered_ago=2)
        self.assertIsNone(livetv._rival_delivering(a))

    async def test_a_stale_rival_is_not(self):
        a = self._lease("recording", 1, 5)
        self._lease("recording", 2, 5, delivered_ago=livetv._RIVAL_STREAMING_FRESH + 5)
        self.assertIsNone(livetv._rival_delivering(a))

    async def test_a_viewer_never_takes_the_account_from_a_recording(self):
        v = self._lease("live", 1, 5)
        rec = self._lease("recording", 2, 5, delivered_ago=1)
        self.assertIs(rec, livetv._rival_delivering(v))

    async def test_a_recording_takes_it_back_from_a_viewer(self):
        rec = self._lease("recording", 1, 5)
        self._lease("live", 2, 5, delivered_ago=1)
        saved = livetv._recording_cache.get("ok_at", -1e9)
        # Jellyfin answered well after the viewer opened: it is a viewer
        livetv._recording_cache["ok_at"] = asyncio.get_running_loop().time() + livetv._CLASSIFY_GRACE + 1
        try:
            self.assertIsNone(livetv._rival_delivering(rec))
        finally:
            livetv._recording_cache["ok_at"] = saved

    async def test_viewers_are_rivals_of_each_other(self):
        v1 = self._lease("live", 1, 5)
        v2 = self._lease("live", 2, 5, delivered_ago=1)
        self.assertIs(v2, livetv._rival_delivering(v1))

    async def test_a_preempted_or_unknown_account_is_not(self):
        a = self._lease("recording", 1, 5)
        b = self._lease("recording", 2, 5, delivered_ago=1)
        b.preempted = True
        self.assertIsNone(livetv._rival_delivering(a))
        unknown = self._lease("recording", 3, None)
        self.assertIsNone(livetv._rival_delivering(unknown))

    async def test_delivering_is_recorded_by_status_set(self):
        a = self._lease("recording", 1, 5)
        b = self._lease("recording", 2, 5)
        b.started -= 100            # long open, never delivered
        self.assertIsNone(livetv._rival_delivering(a))
        livetv._status_set(2, "streaming")
        self.assertIs(b, livetv._rival_delivering(a))
        livetv._status_set(2, "reconnecting", "509")
        self.assertIs(b, livetv._rival_delivering(a), "fresh for _RIVAL_FRESH after its last success")


class WaitingFor(unittest.IsolatedAsyncioTestCase):
    async def test_the_streams_list_says_why_a_stream_is_silent(self):
        panel = NewestWins()
        rival_obj = MagicMock(kind="recording", owner="channel:8")
        seen = {}

        def script(got, p):
            if len(got) == 3:
                p.kill()

        async def watch():
            for _ in range(200):
                st = livetv._stream_status.get(7)
                if st and st.get("waiting_for"):
                    seen["w"] = st["waiting_for"]
                    return
                await _REAL_SLEEP(0.01)
        task = asyncio.create_task(watch())
        await _run(panel, script, rival=lambda: rival_obj, seconds=1.5)
        await task
        self.assertEqual("another recording on this account is delivering", seen.get("w"))


class RawTsUntouched(unittest.IsolatedAsyncioTestCase):
    async def test_a_raw_ts_stream_never_asks_for_a_rival(self):
        from test_livetv_open_single_fetch import PANEL, TOKENIZED, FakeClient, _redirect, _resp
        from test_livetv_raw_reconnect import _dropped, _live
        script = {PANEL: [_redirect(), _resp(509, PANEL), _resp(509, PANEL), _redirect(), _resp(404, PANEL)],
                  TOKENIZED: [_live([b"AAAA"], then=_dropped()), _live([b"BBBB"])]}
        log, closed = [], []
        calls = []
        real_sleep = asyncio.sleep

        async def fast_sleep(d):
            await real_sleep(0)
        with patch("httpx.AsyncClient", lambda **kw: FakeClient(script, log, closed, **kw)), \
                patch("routers.livetv.is_safe_url", lambda *a, **k: True), \
                patch("asyncio.sleep", fast_sleep), \
                patch.object(livetv, "_REVIVE_AFTER", 0.0):
            resp = await livetv._stream_proxy_inner(
                channel_id=1, user_agent="UA", stream_url=PANEL, _release_sem=lambda: None,
                guard=None, is_recording=lambda: True, rival_delivering=lambda: calls.append(1))
            body = b""
            async for piece in resp.body_iterator:
                body += piece
        self.assertEqual(b"AAAABBBB", body)
        self.assertEqual([], calls)



class Review4(unittest.IsolatedAsyncioTestCase):
    """Round-4 review of 4b6ff49 (acct_harness P2-P6)."""

    def setUp(self):
        self.saved = dict(livetv._stream_slots.leases)
        livetv._stream_slots.leases.clear()
        self.saved_status = dict(livetv._stream_status)
        livetv._stream_status.clear()

    def tearDown(self):
        livetv._stream_slots.leases.clear()
        livetv._stream_slots.leases.update(self.saved)
        livetv._stream_status.clear()
        livetv._stream_status.update(self.saved_status)

    async def test_q1_a_raw_ts_stream_keeps_marking_itself_delivering(self):
        """P2: the raw TS generator set "streaming" once, so after 20 s an
        HLS revive saw no rival and kicked a delivering TS recording."""
        from test_livetv_open_single_fetch import PANEL, TOKENIZED, FakeClient, _redirect, _resp

        class SlowBody(httpx.AsyncByteStream):
            async def __aiter__(self):
                for _ in range(12):
                    await _REAL_SLEEP(0.03)
                    yield b"G" + b"\x00" * 187

            async def aclose(self):
                pass
        live = httpx.Response(200, headers=TS, stream=SlowBody(), request=httpx.Request("GET", TOKENIZED))
        script = {PANEL: [_redirect(), _resp(404, PANEL)], TOKENIZED: [live]}
        marks = []
        real_set = livetv._status_set

        def spy(cid, state, last_error=None):
            if state == "streaming":
                marks.append(cid)
            return real_set(cid, state, last_error)
        log, closed = [], []
        with patch("httpx.AsyncClient", lambda **kw: FakeClient(script, log, closed, **kw)), \
                patch("routers.livetv.is_safe_url", lambda *a, **k: True), \
                patch.object(livetv, "_status_set", spy), \
                patch.object(livetv, "_RAW_MARK_EVERY", 0.05), \
                patch.object(livetv, "log_activity", lambda *a, **k: None), \
                patch.object(livetv, "SessionLocal", MagicMock()):
            resp = await livetv._stream_proxy_inner(
                channel_id=3, user_agent="UA", stream_url=PANEL, _release_sem=lambda: None,
                guard=None, is_recording=lambda: True)
            async for _ in resp.body_iterator:
                pass
        self.assertGreaterEqual(len(marks), 4, f"marked delivering {len(marks)} time(s) in 0.36 s")

    async def test_fuzz_seed_7173_a_dropped_raw_stream_counts_from_its_last_bytes(self):
        """Seed 7173: a raw TS recording's connection dropped; its last
        periodic mark was up to _RAW_MARK_EVERY older than its last bytes,
        so a waiting HLS stream judged it stale that much too early."""
        from test_livetv_open_single_fetch import PANEL, TOKENIZED, FakeClient, _redirect, _resp
        from test_livetv_raw_reconnect import _dropped

        class Body(httpx.AsyncByteStream):
            async def __aiter__(self):
                for _ in range(6):
                    await _REAL_SLEEP(0.05)
                    yield b"G" + b"\x00" * 187
                raise _dropped()

            async def aclose(self):
                pass
        live = httpx.Response(200, headers=TS, stream=Body(), request=httpx.Request("GET", TOKENIZED))
        script = {PANEL: [_redirect(), _resp(404, PANEL)], TOKENIZED: [live]}
        seen = {}
        real_set = livetv._status_set

        def spy(cid, state, last_error=None):
            if state == "reconnecting" and "gap" not in seen:
                st = livetv._stream_status.get(cid) or {}
                seen["gap"] = asyncio.get_running_loop().time() - (st.get("last_ok") or 0)
            return real_set(cid, state, last_error)
        log, closed = [], []
        with patch("httpx.AsyncClient", lambda **kw: FakeClient(script, log, closed, **kw)), \
                patch("routers.livetv.is_safe_url", lambda *a, **k: True), \
                patch.object(livetv, "_status_set", spy), \
                patch.object(livetv, "_RAW_MARK_EVERY", 10.0), \
                patch.object(livetv, "log_activity", lambda *a, **k: None), \
                patch.object(livetv, "SessionLocal", MagicMock()):
            resp = await livetv._stream_proxy_inner(
                channel_id=3, user_agent="UA", stream_url=PANEL, _release_sem=lambda: None,
                guard=None, is_recording=lambda: True)
            async for _ in resp.body_iterator:
                pass
        self.assertLess(seen["gap"], 0.1, f"last_ok was {seen['gap']:.2f}s before the drop")

    async def test_q1_hls_marks_every_segment(self):
        marks = []
        real_set = livetv._status_set

        def spy(cid, state, last_error=None):
            if state == "streaming":
                marks.append(cid)
            return real_set(cid, state, last_error)
        with patch.object(livetv, "_status_set", spy):
            got, _, _ = await _run(NewestWins(), lambda g, p: None, rival=lambda: None, seconds=0.8)
        self.assertGreaterEqual(len(marks), len(got), (len(marks), len(got)))

    def _lease(self, kind, channel_id, provider_id, account=None, delivered_ago=None, state="streaming"):
        lease = livetv._stream_slots._grant(kind, f"channel:{channel_id}")
        lease.provider_id, lease.channel_id, lease.account = provider_id, channel_id, account
        if delivered_ago is not None:
            now = asyncio.get_running_loop().time()
            livetv._stream_status[channel_id] = {"state": state, "since": now, "opened_at": now,
                                                 "last_error": None, "last_ok": now - delivered_ago}
        return lease

    async def test_q2_two_provider_rows_for_one_account_are_rivals(self):
        a = self._lease("recording", 1, 1, ("panel.example", "u"))
        b = self._lease("recording", 4, 2, ("panel.example", "u"), delivered_ago=1)
        self.assertIs(b, livetv._rival_delivering(a))

    async def test_q2_another_username_on_the_same_host_is_not(self):
        a = self._lease("recording", 1, 1, ("panel.example", "u"))
        self._lease("recording", 4, 2, ("panel.example", "other"), delivered_ago=1)
        self.assertIsNone(livetv._rival_delivering(a))

    async def test_q2_the_account_key(self):
        P = MagicMock
        k = livetv._account_key
        self.assertEqual(k(P(server_url="http://Panel.Example:8080/", username="u")),
                         k(P(server_url="https://www.panel.example", username=" u ")))
        self.assertNotEqual(k(P(server_url="http://panel.example", username="u")),
                            k(P(server_url="http://panel.example", username="v")))
        self.assertIsNone(k(P(server_url="", username="u")))
        self.assertIsNone(k(None))

    async def test_fuzz_seed_34_a_pull_not_yet_classified_counts_as_a_recording(self):
        """Randomised run, seed 34: a TS recording had opened 4 s earlier and
        was still classified "live" (Jellyfin marks the timer InProgress only
        after the open); a waiting HLS recording re-resolved against it and
        ended its session."""
        saved = dict(livetv._recording_cache)
        try:
            a = self._lease("recording", 1, 1)
            livetv._recording_cache["ok_at"] = asyncio.get_running_loop().time() - 1.0
            b = self._lease("live", 2, 1, delivered_ago=0.5)       # opened after the last lookup
            self.assertIs(b, livetv._rival_delivering(a))
            # answered, but too soon after the open to be sure
            livetv._recording_cache["ok_at"] = asyncio.get_running_loop().time()
            self.assertIs(b, livetv._rival_delivering(a))
            livetv._recording_cache["ok_at"] = (asyncio.get_running_loop().time()
                                                + livetv._CLASSIFY_GRACE + 1)   # answered later: a viewer
            self.assertIsNone(livetv._rival_delivering(a))
        finally:
            livetv._recording_cache.clear()
            livetv._recording_cache.update(saved)

    async def test_fuzz_seeds_110_197_a_rival_still_opening_holds_the_account(self):
        """Two recordings opened within a second: the first one's session was
        ended while it opened, and its Q5 re-resolve took the account from
        the second, which had just been handed it (no status yet)."""
        a = self._lease("recording", 1, 1)
        b = self._lease("recording", 2, 1)          # opening: no status entry yet
        self.assertIs(b, livetv._rival_delivering(a))
        b.started -= livetv._RIVAL_FRESH + 1          # an open that never delivered
        self.assertIsNone(livetv._rival_delivering(a))

    async def test_fuzz_seed_234_a_stream_re_resolving_now_holds_the_account(self):
        """Two recordings waited behind an outside player; both saw no
        rival and re-resolved in the same instant, the second taking the
        account straight back from the first."""
        a = self._lease("recording", 1, 1, delivered_ago=30, state="reconnecting")
        b = self._lease("recording", 2, 1, delivered_ago=30, state="reconnecting")
        self.assertIsNone(livetv._rival_delivering(b))
        livetv._stream_status[1]["reviving_at"] = asyncio.get_running_loop().time()
        self.assertIs(a, livetv._rival_delivering(b))

    async def test_fuzz_seed_234_the_claim_is_made_before_the_request(self):
        panel = NewestWins()
        real = panel.route
        claims = []

        def route(url):
            if url == CHANNEL and panel.issued >= 1:
                st = livetv._stream_status.get(7) or {}
                claims.append(st.get("reviving_at"))
            return real(url)
        panel.route = route
        await _run(panel, _kill_at(3), rival=lambda: None, seconds=2.0)
        self.assertEqual(1, len(claims), claims)
        self.assertIsNotNone(claims[0], "the revive did not claim the account before asking for it")

    async def test_fuzz_seed_197_q5_is_decided_again_right_before_the_re_walk(self):
        panel = NewestWins()
        panel.kill_first = True
        state = {"n": 0}
        rival_obj = MagicMock(kind="recording", owner="channel:2")

        def rival():
            state["n"] += 1
            return None if state["n"] == 1 else rival_obj   # a rival appears after the first check
        from fastapi import HTTPException
        with self.assertRaises(HTTPException):
            await _run(panel, lambda g, p: None, rival=rival, seconds=0.8)
        self.assertEqual(1, len(panel.resolves), "re-resolved against a rival that appeared meanwhile")

    async def test_fuzz_seed_157_a_fresh_lookup_is_awaited_before_a_revive(self):
        panel = NewestWins()
        calls = []

        async def refresh():
            calls.append(asyncio.get_running_loop().time())
        real = livetv._stream_proxy_inner

        async def with_refresh(*a, **k):
            return await real(*a, refresh_recordings=refresh, **k)
        with patch.object(livetv, "_stream_proxy_inner", with_refresh):
            await _run(panel, _kill_at(3), rival=lambda: None, seconds=2.0)
        self.assertEqual(2, len(panel.resolves))
        self.assertGreaterEqual(len(calls), 1, "no fresh recording lookup before the revive")

    def _lookup_done(self, sids):
        """Apply one finished Jellyfin lookup the way the app does."""
        fut = asyncio.get_running_loop().create_future()
        if isinstance(sids, Exception):
            fut.set_exception(sids)
        else:
            fut.set_result(sids)
        livetv._recording_cache["pending"] = fut
        livetv._recording_lookup_done(fut)

    async def test_fuzz_seed_7007_a_failed_lookup_does_not_classify_a_new_pull(self):
        """Review of 1eb5ee7, M-1: a failed Jellyfin lookup advanced the
        cache time, so a just-opened recording counted as a viewer and a
        waiting recording's revive ended it (seed 7007, lookups failing)."""
        saved = dict(livetv._recording_cache)
        try:
            livetv._recording_cache.pop("ok_at", None)
            a = self._lease("recording", 1, 1)
            b = self._lease("live", 2, 1, delivered_ago=1)
            b.started -= livetv._CLASSIFY_GRACE + 5          # opened a while ago ...
            self._lookup_done(RuntimeError("Jellyfin down"))  # ... and every lookup since failed
            self.assertIs(b, livetv._rival_delivering(a))
            self._lookup_done(set())                          # Jellyfin answered: not recording
            self.assertIsNone(livetv._rival_delivering(a))
        finally:
            livetv._recording_cache.clear()
            livetv._recording_cache.update(saved)

    async def test_fuzz_seed_7095_a_lookup_just_after_the_open_does_not_classify_it(self):
        """Seed 7095, lookups lagging: Jellyfin answered right after the
        recording's tuner opened, before it marked the timer InProgress."""
        saved = dict(livetv._recording_cache)
        try:
            a = self._lease("recording", 1, 1)
            b = self._lease("live", 2, 1, delivered_ago=1)
            self._lookup_done(set())                          # answered 0 s after b opened
            self.assertIs(b, livetv._rival_delivering(a))
        finally:
            livetv._recording_cache.clear()
            livetv._recording_cache.update(saved)

    async def test_q4_a_rival_still_streaming_is_delivering_through_a_slow_segment(self):
        a = self._lease("recording", 1, 1)
        b = self._lease("recording", 2, 1, delivered_ago=40, state="streaming")
        self.assertIs(b, livetv._rival_delivering(a))

    async def test_q4_but_not_once_it_is_reconnecting_or_older_than_a_minute(self):
        a = self._lease("recording", 1, 1)
        self._lease("recording", 2, 1, delivered_ago=40, state="reconnecting")
        self._lease("recording", 3, 1, delivered_ago=70, state="streaming")
        self.assertIsNone(livetv._rival_delivering(a))

    async def test_q3_brief_delivery_does_not_reset_the_cooldown(self):
        """P3: an outside player that re-opens after each kick was fought
        every 60 s, because a few segments reset the cooldown."""
        panel = NewestWins()
        state = {"since_open": 0}

        def script(got, p):
            # every fresh session is killed again after two segments
            if p.issued != state.get("tok"):
                state["tok"], state["since_open"] = p.issued, 0
            state["since_open"] += 1
            if state["since_open"] == 2:
                p.kill()
        await _run(panel, script, rival=lambda: None, seconds=6.0)
        gaps = [b - a for a, b in zip(panel.resolves[1:], panel.resolves[2:])]
        self.assertGreaterEqual(len(gaps), 2, panel.resolves)
        self.assertGreaterEqual(gaps[0], COOLDOWN * 0.95, gaps)
        self.assertGreaterEqual(gaps[1], 2 * COOLDOWN * 0.95, f"the cooldown was reset by a few segments: {gaps}")

    async def test_q3_long_delivery_resets_it(self):
        panel = NewestWins()
        def script(got, p):
            if len(got) in (3, 40):
                p.kill()
        with patch.object(livetv, "_REVIVE_RESET_AFTER", 0.3):
            got, ended, last = await _run(panel, script, rival=lambda: None, seconds=8.0)
        self.assertEqual(3, len(panel.resolves), panel.resolves)
        self.assertEqual(2, last["revives"], last)

    async def test_q5_a_session_ended_during_the_open_is_resolved_once_more(self):
        """P5: the open retried the dead token URL for 20 s and failed 502."""
        panel = NewestWins()
        panel.kill_first = True
        got, ended, last = await _run(panel, lambda g, p: None, rival=lambda: None, seconds=0.8)
        self.assertEqual(2, len(panel.resolves), panel.resolves)
        self.assertGreater(len(got), 2)

    async def test_q5_not_against_a_delivering_rival(self):
        from fastapi import HTTPException
        panel = NewestWins()
        panel.kill_first = True
        rival_obj = MagicMock(kind="recording", owner="channel:8")
        with self.assertRaises(HTTPException) as cm:
            await _run(panel, lambda g, p: None, rival=lambda: rival_obj, seconds=0.8)
        self.assertEqual(502, cm.exception.status_code)
        self.assertEqual(1, len(panel.resolves))

    async def test_q5_without_the_callable_nothing_changes(self):
        from fastapi import HTTPException
        panel = NewestWins()
        panel.kill_first = True
        with self.assertRaises(HTTPException):
            await _run(panel, lambda g, p: None, pass_rival=False, seconds=0.8)
        self.assertEqual(1, len(panel.resolves))


if __name__ == "__main__":
    unittest.main()
