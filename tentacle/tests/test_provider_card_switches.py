"""The provider card's switches and Health's log pages.

Run from the tentacle/ directory:  python -m unittest discover -s tests

Sources shows each IPTV provider as a card with two switches, "Use for
Movies & Series" (`active`, the VOD sync switch) and "Use for Live TV"
(`live_tv_enabled`). Both go through PUT /api/providers/{id}.

- Live TV off takes the provider's channels out of the tuner: the lineup,
  M3U and guide list enabled channels whatever the provider's switch says,
  so its groups and channels are switched off.
- A re-test used to switch Live TV back on whenever the provider has live
  channels; now only the first time they are found.
- Health's log asks for the last N days, a page at a time.
"""
import unittest
from datetime import datetime, timedelta
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from tmp_dirs import temp_dir


class _Base(unittest.TestCase):
    def setUp(self):
        import models.database as mdb
        engine = create_engine(f"sqlite:///{temp_dir(self)}/t.db", connect_args={"check_same_thread": False})
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)
        self.mdb = mdb


class LiveTvSwitch(_Base):
    def setUp(self):
        super().setUp()
        mdb = self.mdb
        self.p = mdb.Provider(name="p", server_url="http://prov.test", username="a", password="b",
                              has_live=True, live_tv_enabled=True, active=True)
        self.other = mdb.Provider(name="q", server_url="http://other.test", username="a", password="b",
                                  has_live=True, live_tv_enabled=True)
        self.db.add_all([self.p, self.other])
        self.db.commit()
        for prov in (self.p, self.other):
            self.db.add(mdb.LiveChannelGroup(provider_id=prov.id, name="News", category_id="1", enabled=True))
            self.db.add(mdb.LiveChannel(provider_id=prov.id, name="CBC", stream_url="http://x.test/1.ts",
                                        group_title="News", enabled=True))
        self.db.commit()

    def _put(self, provider, **body):
        from routers import providers
        with mock.patch("routers.livetv.rewrite_xtream_channel_urls"):
            providers.update_provider(provider.id, providers.ProviderUpdate(**body), self.db)
        self.db.expire_all()

    def _enabled(self, provider):
        mdb = self.mdb
        ch = self.db.query(mdb.LiveChannel).filter_by(provider_id=provider.id, enabled=True).count()
        gr = self.db.query(mdb.LiveChannelGroup).filter_by(provider_id=provider.id, enabled=True).count()
        return ch, gr

    def test_off_takes_its_channels_out_of_the_tuner_and_leaves_others(self):
        self._put(self.p, live_tv_enabled=False)
        self.assertFalse(self.db.get(self.mdb.Provider, self.p.id).live_tv_enabled)
        self.assertEqual((0, 0), self._enabled(self.p))
        self.assertEqual((1, 1), self._enabled(self.other))

    def test_movies_switch_alone_leaves_live_tv_alone(self):
        self._put(self.p, active=False)
        p = self.db.get(self.mdb.Provider, self.p.id)
        self.assertEqual((False, True), (p.active, p.live_tv_enabled))
        self.assertEqual((1, 1), self._enabled(self.p))

    def test_on_again_keeps_the_choice_empty(self):
        self._put(self.p, live_tv_enabled=False)
        self._put(self.p, live_tv_enabled=True)
        self.assertTrue(self.db.get(self.mdb.Provider, self.p.id).live_tv_enabled)
        self.assertEqual((0, 0), self._enabled(self.p))


class RetestKeepsTheSwitch(_Base):
    def _test(self, provider):
        from routers import providers

        class Resp:
            def json(self):
                return [{"category_id": "1"}]

        class Sess:
            headers = {}

            def get(self, *a, **k):
                return Resp()

        data = {"user_info": {"exp_date": None, "max_connections": "2", "status": "Active"}}
        with mock.patch.object(providers, "test_provider_connection", return_value=data), \
                mock.patch.object(providers.requests, "Session", Sess), \
                mock.patch("services.provider_activity.refuse_while_recording"):
            providers.test_provider(provider.id, self.db)
        self.db.expire_all()
        return self.db.get(self.mdb.Provider, provider.id)

    def test_first_detection_switches_live_tv_on(self):
        p = self.mdb.Provider(name="p", server_url="http://prov.test", username="a", password="b")
        self.db.add(p)
        self.db.commit()
        self.assertTrue(self._test(p).live_tv_enabled)

    def test_a_retest_leaves_live_tv_off(self):
        p = self.mdb.Provider(name="p", server_url="http://prov.test", username="a", password="b",
                              has_live=True, live_tv_enabled=False)
        self.db.add(p)
        self.db.commit()
        self.assertFalse(self._test(p).live_tv_enabled)


class LogPages(_Base):
    def setUp(self):
        super().setUp()
        now = datetime.utcnow()
        for i in range(6):
            when = now - timedelta(days=2 * i, minutes=1)
            self.db.add(self.mdb.DeletionLog(kind="sync-prune", name=f"d{i}", reason="auto", created_at=when))
            self.db.add(self.mdb.ActivityLog(event="vod_sync", message=f"a{i}", created_at=when))
        self.db.commit()

    def test_deletions_last_days_and_pages(self):
        from routers.health import get_deletions
        names = lambda **kw: [e["name"] for e in get_deletions(db=self.db, **kw)]
        self.assertEqual(["d0", "d1", "d2", "d3"], names(days=7))
        self.assertEqual(["d2", "d3"], names(days=7, limit=2, offset=2))
        self.assertEqual(6, len(names()))
        self.assertEqual(["d0"], names(days=7, limit=1, offset=-5))

    def test_activity_last_days_and_pages(self):
        from routers.sync import get_activity
        msgs = lambda **kw: [e["message"] for e in get_activity(db=self.db, **kw)]
        self.assertEqual(["a0", "a1", "a2", "a3"], msgs(days=7, limit=50))
        self.assertEqual(["a3"], msgs(days=7, limit=50, offset=3))
        self.assertEqual(6, len(msgs(limit=50)))


if __name__ == "__main__":
    unittest.main()
