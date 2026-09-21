"""The concurrent-stream ceiling is a setting, waits briefly for a slot, and a
refusal is visible (#87).

Run from the tentacle/ directory:  python -m unittest discover -s tests

At 9bde42e the ceiling is a module constant baked into an asyncio.Semaphore, a
full house is refused instantly even if a slot frees a moment later, and the
only trace is one WARNING naming a channel id -- which is how a scheduled
recording turns into a zero-byte file nobody can explain.
"""
import asyncio
import tempfile
import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


class _Base(unittest.TestCase):
    def setUp(self):
        import models.database as mdb
        engine = create_engine(f"sqlite:///{tempfile.mkdtemp()}/t.db",
                               connect_args={"check_same_thread": False})
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)


class CeilingIsASetting(_Base):
    def _limit(self):
        from routers import livetv
        return livetv._max_concurrent_streams(self.db)

    def test_default_is_unchanged(self):
        self.assertEqual(6, self._limit())

    def test_admin_can_raise_it(self):
        from models.database import set_setting
        set_setting(self.db, "livetv_max_concurrent_streams", "12")
        self.db.commit()
        self.assertEqual(12, self._limit())

    def test_zero_means_unlimited(self):
        from models.database import set_setting
        set_setting(self.db, "livetv_max_concurrent_streams", "0")
        self.db.commit()
        self.assertEqual(0, self._limit())

    def test_garbage_keeps_the_default_rather_than_removing_the_cap(self):
        from models.database import set_setting
        for bad in ("lots", "-3x", " "):
            set_setting(self.db, "livetv_max_concurrent_streams", bad)
            self.db.commit()
            self.assertEqual(6, self._limit(), bad)


class Slots(unittest.TestCase):
    def _slots(self):
        from routers import livetv
        return livetv._StreamSlots()

    def test_limit_is_enforced(self):
        async def go():
            s = self._slots()
            got = [await s.acquire(2, 0.01) for _ in range(3)]
            return got, s.active
        got, active = asyncio.run(go())
        self.assertEqual([True, True, False], got)
        self.assertEqual(2, active)

    def test_unlimited_never_refuses(self):
        async def go():
            s = self._slots()
            return all([await s.acquire(0, 0) for _ in range(50)])
        self.assertTrue(asyncio.run(go()))

    def test_a_slot_freed_during_the_wait_is_taken_instead_of_refusing(self):
        """One recording ending as the next begins: the old code 503'd the new
        one on the spot."""
        async def go():
            s = self._slots()
            await s.acquire(1, 0)
            asyncio.get_running_loop().call_later(0.05, s.release)
            return await s.acquire(1, 1.0), s.active
        ok, active = asyncio.run(go())
        self.assertTrue(ok)
        self.assertEqual(1, active)

    def test_raising_the_limit_takes_effect_without_a_restart(self):
        async def go():
            s = self._slots()
            await s.acquire(1, 0)
            return await s.acquire(1, 0.01), await s.acquire(2, 0.01)
        self.assertEqual((False, True), asyncio.run(go()))

    def test_release_never_goes_negative(self):
        s = self._slots()
        s.release(); s.release()
        self.assertEqual(0, s.active)


class RefusalIsVisible(_Base):
    def test_refusal_names_the_channel_is_an_error_and_is_counted(self):
        from fastapi import HTTPException
        from models.database import LiveChannel, Provider, set_setting
        from routers import livetv

        self.db.add(Provider(id=1, name="p", server_url="http://example.com", username="u", password="p"))
        self.db.add(LiveChannel(id=7, provider_id=1, name="TSN 4", stream_url="http://example.com/7.ts"))
        set_setting(self.db, "livetv_max_concurrent_streams", "1")
        self.db.commit()

        slots = livetv._StreamSlots()
        orig, orig_wait = livetv._stream_slots, livetv._SLOT_WAIT_SECONDS
        livetv._stream_slots, livetv._SLOT_WAIT_SECONDS = slots, 0.01
        self.addCleanup(lambda: (setattr(livetv, "_stream_slots", orig),
                                 setattr(livetv, "_SLOT_WAIT_SECONDS", orig_wait)))

        async def go():
            await slots.acquire(1, 0)      # the house is full
            with self.assertLogs("routers.livetv", level="ERROR") as logs:
                with self.assertRaises(HTTPException) as ctx:
                    await livetv.stream_proxy(7, db=self.db)
            return ctx.exception, "\n".join(logs.output)

        exc, text = asyncio.run(go())
        self.assertEqual(503, exc.status_code)
        self.assertIn("TSN 4", text, "the refusal must name the channel, not just its id")
        self.assertEqual(1, slots.refused)
        self.assertEqual("TSN 4", slots.last_refused["channel"])
        self.assertEqual(1, slots.active, "a refused request must not leak a slot")
        self.assertEqual(1, livetv.live_capacity(db=self.db)["refused_since_start"])


if __name__ == "__main__":
    unittest.main()
