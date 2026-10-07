"""An EPG sync still completes when a channel is removed while it parses the feed.

Run from the tentacle/ directory:
    python tests/hermetic.py discover -s tests -p test_epg_sync_channel_removed.py

An M3U playlist sync can remove a channel while the guide sync runs
(sync_live_groups only refuses while the phase is "running"). Up to 6c0f768 the
guide sync completed. Since 8490718 (#466) it reads every channel row's
guide_epg_id after the coverage setting has committed, which expires the rows:
the removed one fails on reload, the sync ends with status "error" ("Instance
... has been deleted"), and its success entry, the refresh-guide record and (for
the nightly sync) the Jellyfin guide refresh are all skipped, although the new
guide was already committed.
"""
import logging
import os
import shutil
import unittest
from datetime import datetime, timedelta
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb
import routers.livetv as livetv_router
import services.xmltv as xmltv
from tmp_dirs import temp_dir


def setUpModule():
    logging.disable(logging.CRITICAL)


def tearDownModule():
    logging.disable(logging.NOTSET)


def _stamp(dt):
    return dt.strftime("%Y%m%d%H%M%S +0000")


class ChannelRemovedDuringGuideSync(unittest.TestCase):
    def setUp(self):
        self.tmp = temp_dir(self)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        engine = create_engine(f"sqlite:///{self.tmp}/t.db")
        self.addCleanup(engine.dispose)
        mdb.Base.metadata.create_all(engine)
        self.Session = sessionmaker(bind=engine)
        db = self.Session()
        p = mdb.Provider(name="P", server_url="http://192.0.2.10", username="u", password="p",
                         live_tv_enabled=True)
        db.add(p)
        db.commit()
        self.pid = p.id
        for sid, name, tvg in (("1", "CA: AMC HD", "AMC.ca"), ("2", "Gone Channel", "gone.example")):
            db.add(mdb.LiveChannel(provider_id=p.id, name=name, stream_id=sid, epg_channel_id=tvg,
                                   stream_url=f"http://192.0.2.10/live/{sid}.ts", enabled=True))
        db.commit()
        db.close()
        patcher = mock.patch.object(xmltv, "XMLTV_CACHE_DIR", os.path.join(self.tmp, "cache"))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.url = "http://192.0.2.10/xmltv.php"
        start = datetime.utcnow() + timedelta(hours=1)
        with open(xmltv._get_cache_path(self.url), "w", encoding="utf-8") as f:
            f.write('<tv><channel id="AMC.ca"><display-name>AMC</display-name></channel>'
                    f'<programme start="{_stamp(start)}" stop="{_stamp(start + timedelta(hours=1))}" '
                    'channel="AMC.ca"><title>A film</title></programme></tv>')

    def test_the_sync_completes(self):
        real = xmltv.stream_parse_xmltv

        def parse(*a, **k):
            out = real(*a, **k)
            s = self.Session()
            s.query(mdb.LiveChannel).filter_by(stream_id="2").delete()
            s.commit()
            s.close()
            return out

        data = {"id": self.pid, "channels": [], "enabled_count": 2, "epg_url": self.url,
                "provider_type": "xtream", "user_agent": "t"}
        with mock.patch.object(xmltv, "stream_parse_xmltv", parse), \
                mock.patch.object(livetv_router, "SessionLocal", self.Session):
            ok = livetv_router._run_epg_sync_background(data)
        self.assertTrue(ok, livetv_router._get_sync_status(self.pid))
        db = self.Session()
        try:
            self.assertEqual(1, db.query(mdb.EPGProgram).filter_by(channel_id="AMC.ca").count())
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
