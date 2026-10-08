"""An EPG sync leaves another provider's guide alone (follow-up to #467).

Two Live TV providers whose feeds share guide ids (the same XMLTV source, or
one EPG URL on both). Provider P's channel "CA: TSN 5" has its own tvg-id
programmes and also name-matches the feed's TSN5.ca; #141 drops that match.
#467 now deletes every matched id, dropped ones included, and keeps no
programme for them -- so P's EPG sync wipes TSN5.ca, the guide provider Q's
channel uses by tvg-id, and Q's channel shows no guide until Q's next EPG sync.
Before #467 the same P sync failed on UNIQUE (Q's rows intact).

Run from tentacle/:  python tests/hermetic.py discover -s tests -p test_epg_shared_guide_id.py -k shared
"""
import unittest
from unittest import mock

import models.database as mdb
import routers.livetv as livetv_router
from tests.test_epg_name_matching import EpgSyncMatchesByName


class SharedGuideId(EpgSyncMatchesByName):
    def _sync_pid(self, pid):
        data = {"id": pid, "channels": [], "enabled_count": 1, "epg_url": self.url,
                "provider_type": "xtream", "user_agent": "t"}
        with mock.patch.object(livetv_router, "SessionLocal", self.Session):
            return livetv_router._run_epg_sync_background(data)

    def test_shared_guide_id_of_another_provider_survives(self):
        q = mdb.Provider(name="Q", server_url="http://192.0.2.20", username="u", password="p",
                         live_tv_enabled=True)
        self.db.add(q)
        self.db.commit()
        self.db.add(mdb.LiveChannel(provider_id=q.id, name="TSN 5 HD", stream_id="9", epg_channel_id="TSN5.ca",
                                    stream_url="http://192.0.2.20/live/9.ts", enabled=True))
        self.db.commit()
        qid = q.id
        self._add_own_tsn_schedule()          # P's TSN 5 has its own tvg-id programmes: its match is dropped
        self.assertTrue(self._sync_pid(qid))
        self.db.expire_all()
        self.assertEqual(1, self.db.query(mdb.EPGProgram).filter_by(channel_id="TSN5.ca").count(),
                         "Q's channel has its guide")
        self.assertTrue(self._sync_pid(self.pid), livetv_router._get_sync_status(self.pid).get("message"))
        self.db.expire_all()
        self.assertEqual(1, self.db.query(mdb.EPGProgram).filter_by(channel_id="TSN5.ca").count(),
                         "provider P's EPG sync deleted the guide of provider Q's channel")


def load_tests(loader, tests, pattern):
    """Only this file's own tests, not the ones its base classes bring."""
    suite = unittest.TestSuite()
    for cls in (SharedGuideId,):
        suite.addTests(cls(name) for name in loader.getTestCaseNames(cls) if name in vars(cls))
    return suite
