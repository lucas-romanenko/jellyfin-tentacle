"""How long a running stream waits out a failing upstream is a setting, and
0 means "until the client leaves" (#136).

Run from the tentacle/ directory:  python -m unittest discover -s tests

At 2f21e21 the budget is a constant of 120 s in both the raw-TS re-dial loop
and the HLS worker. Live on 2026-09-23: the provider answered a recording's
requests with 509 for 160 s; the pump gave up at 121 s and the provider was
accepting again 18 s later. Jellyfin never appends to a recording once its
tuner stream closes, so the game came out as four files. The recorder is the
one client that stays connected for as long as it matters -- a viewer who
gives up closes the socket, which ends the retries anyway.
"""
import asyncio
import tempfile
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from test_livetv_open_single_fetch import PANEL, TOKENIZED, FakeClient, _redirect, _resp
from test_livetv_raw_reconnect import _dropped, _live


class BudgetIsASetting(unittest.TestCase):
    def setUp(self):
        import models.database as mdb
        engine = create_engine(f"sqlite:///{tempfile.mkdtemp()}/t.db",
                               connect_args={"check_same_thread": False})
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)

    def _budget(self):
        from routers import livetv
        return livetv._reconnect_budget(self.db)

    def test_default_is_the_old_constant(self):
        self.assertEqual(120.0, self._budget())

    def test_zero_means_until_the_client_leaves(self):
        from models.database import set_setting
        set_setting(self.db, "livetv_reconnect_budget_seconds", "0")
        self.db.commit()
        self.assertEqual(0.0, self._budget())

    def test_admin_can_set_it(self):
        from models.database import set_setting
        set_setting(self.db, "livetv_reconnect_budget_seconds", "600")
        self.db.commit()
        self.assertEqual(600.0, self._budget())

    def test_garbage_keeps_the_default(self):
        from models.database import set_setting
        for bad in ("forever", "-5x", " "):
            set_setting(self.db, "livetv_reconnect_budget_seconds", bad)
            self.db.commit()
            self.assertEqual(120.0, self._budget(), bad)


async def _play(script, failure_budget):
    """Open a raw-TS channel with the given budget and read it to the end."""
    import routers.livetv as livetv
    log, closed, slept = [], [], []
    real_sleep = asyncio.sleep

    async def fast_sleep(delay):
        slept.append(delay)
        await real_sleep(0)

    def factory(**kw):
        return FakeClient(script, log, closed, **kw)

    with patch("httpx.AsyncClient", factory), \
            patch("routers.livetv.is_safe_url", lambda *a, **k: True), \
            patch("asyncio.sleep", fast_sleep):
        response = await livetv._stream_proxy_inner(
            channel_id=1, user_agent="TestAgent/1.0", stream_url=PANEL,
            _release_sem=lambda: None, guard=None, failure_budget=failure_budget)
        pieces = []
        async for piece in response.body_iterator:
            pieces.append(piece)
    return b"".join(pieces), log, slept


class RawStreamHonoursTheBudget(unittest.IsolatedAsyncioTestCase):
    async def test_zero_budget_outlasts_a_long_refusal(self):
        """509 for well over the old 120 s, then the provider is fine again:
        the recording must carry on in the same file."""
        refusals = [_resp(509, PANEL)] * 60      # ~5 s apart once the backoff caps: ~300 s
        script = {
            PANEL: [_redirect()] + refusals + [_redirect(), _resp(404, PANEL)],
            TOKENIZED: [_live([b"AAAA"], then=_dropped()), _live([b"BBBB"])],
        }
        body, log, slept = await _play(script, failure_budget=0)
        self.assertEqual(b"AAAABBBB", body, "gave up: Jellyfin would have started a new file")
        self.assertGreater(sum(slept), 200, "the refusals were not waited out")

    async def test_a_finite_budget_still_gives_up(self):
        script = {
            PANEL: [_redirect(), _resp(509, PANEL)],       # refuses for ever afterwards
            TOKENIZED: [_live([b"AAAA"], then=_dropped())],
        }
        body, log, slept = await _play(script, failure_budget=30)
        self.assertEqual(b"AAAA", body)
        self.assertLessEqual(sum(slept), 40, "kept re-dialling past the configured budget")

    async def test_zero_budget_still_stops_on_a_status_that_will_never_fix_itself(self):
        script = {
            PANEL: [_redirect(), _resp(404, PANEL)],
            TOKENIZED: [_live([b"AAAA"], then=_dropped())],
        }
        body, log, slept = await _play(script, failure_budget=0)
        self.assertEqual(b"AAAA", body)
        self.assertEqual(1, len(slept), "a 404 must not be retried, whatever the budget")


if __name__ == "__main__":
    unittest.main()
