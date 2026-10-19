"""refresh-guide runs the EPG sync first only when that can give a channel a
guide (#466).

An enabled channel whose tvg-id the provider's feed does not carry never has
programmes. refresh-guide used to run the whole sync again, inline, on every
call because of it, right after a sync that read the same feed and channels.
It still runs it when something the sync reads changed since: a channel added
or edited, the cached feed, or when no sync has run yet.
"""
import logging
import os
import shutil
import unittest
from datetime import datetime, timedelta
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb
import routers.livetv as livetv_router
import services.jellyfin_guide as jellyfin_guide
import services.xmltv as xmltv
from tmp_dirs import temp_dir

URL = "http://192.0.2.10/xmltv.php"


def _stamp(dt):
    return dt.strftime("%Y%m%d%H%M%S +0000")


class Inline:
    """threading.Thread that runs its target in start()."""
    def __init__(self, target, args=(), daemon=None):
        self.start = lambda: target(*args)


class RefreshGuideResync(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        self.tmp = temp_dir(self)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        engine = create_engine(f"sqlite:///{self.tmp}/t.db")
        self.addCleanup(engine.dispose)
        mdb.Base.metadata.create_all(engine)
        self.Session = sessionmaker(bind=engine)
        self.db = self.Session()
        self.addCleanup(self.db.close)
        p = mdb.Provider(name="P", server_url="http://192.0.2.10", username="u", password="p",
                         live_tv_enabled=True, epg_url=URL)
        self.db.add(p)
        self.db.commit()
        self.pid = p.id
        # AMC's tvg-id is in the feed; "Gone Channel"'s is not.
        for sid, name, tvg in (("1", "CA: AMC HD", "AMC.ca"), ("2", "Gone Channel", "gone.example")):
            self.add_channel(sid, name, tvg)
        mdb.set_setting(self.db, "jellyfin_url", "http://127.0.0.1:9")
        mdb.set_setting(self.db, "jellyfin_api_key", "k")
        self.db.commit()
        self.jf_refresh = mock.Mock()
        for target, name, value in ((xmltv, "XMLTV_CACHE_DIR", os.path.join(self.tmp, "cache")),
                                    (livetv_router, "SessionLocal", self.Session),
                                    (jellyfin_guide, "refresh_jellyfin_guide", self.jf_refresh)):
            pt = mock.patch.object(target, name, value)
            pt.start()
            self.addCleanup(pt.stop)
        self.write_feed()
        app = FastAPI()
        app.include_router(livetv_router.router)
        app.dependency_overrides[mdb.get_db] = lambda: self.db
        app.dependency_overrides[livetv_router.require_admin] = lambda: None
        self.client = TestClient(app)

    def add_channel(self, sid, name, tvg):
        self.db.add(mdb.LiveChannel(provider_id=self.pid, name=name, stream_id=sid, epg_channel_id=tvg,
                                    stream_url=f"http://192.0.2.10/live/{sid}.ts", enabled=True))
        self.db.commit()

    def write_feed(self, extra=""):
        """The provider's XMLTV, already in the 8 h disk cache: nothing is downloaded."""
        start = datetime.utcnow() + timedelta(hours=1)
        with open(xmltv._get_cache_path(URL), "w", encoding="utf-8") as f:
            f.write(f'<tv><channel id="AMC.ca"><display-name>AMC</display-name></channel>'
                    f'<programme start="{_stamp(start)}" stop="{_stamp(start + timedelta(hours=1))}" '
                    f'channel="AMC.ca"><title>A film</title></programme>{extra}</tv>')

    def sync_epg(self):
        """"Sync EPG" on the Live TV page, its background thread run inline."""
        with mock.patch.object(livetv_router, "threading", mock.Mock(Thread=Inline)):
            livetv_router.sync_epg(self.pid, db=self.db)
        self.assertEqual("complete", livetv_router._get_sync_status(self.pid)["status"])

    def refresh_guide(self, calls=1):
        """POST /api/live/refresh-guide; returns the EPG syncs it ran."""
        real, runs = livetv_router._run_epg_sync_background, []
        with mock.patch.object(livetv_router, "_run_epg_sync_background",
                               lambda data: runs.append(data["id"]) or real(data)):
            for _ in range(calls):
                r = self.client.post("/api/live/refresh-guide")
                self.assertEqual(200, r.status_code, r.text)
        return runs

    def test_right_after_a_sync_it_does_not_run_it_again(self):
        self.sync_epg()
        self.assertEqual([], self.refresh_guide(3), "refresh-guide re-ran the whole EPG sync on every call")
        self.assertEqual(3, self.jf_refresh.call_count)

    def test_before_any_sync_it_still_syncs_first(self):
        self.assertEqual([self.pid], self.refresh_guide())
        # That sync is recorded: the next call goes straight to Jellyfin.
        self.assertEqual([], self.refresh_guide())

    def test_an_override_set_after_the_sync_syncs_again_once(self):
        self.sync_epg()
        ch = self.db.query(mdb.LiveChannel).filter_by(stream_id="2").one()
        ch.epg_id_override = "elsewhere.example"
        self.db.commit()
        self.assertEqual([self.pid], self.refresh_guide())
        self.assertEqual([], self.refresh_guide())

    def test_a_channel_added_after_the_sync_syncs_again(self):
        self.sync_epg()
        self.add_channel("3", "New Channel", "new.example")
        self.assertEqual([self.pid], self.refresh_guide())

    def test_a_new_feed_syncs_again_and_fills_the_channel(self):
        self.sync_epg()
        start = datetime.utcnow() + timedelta(hours=2)
        self.write_feed(f'<programme start="{_stamp(start)}" stop="{_stamp(start + timedelta(hours=1))}" '
                        f'channel="gone.example"><title>Back</title></programme>')
        self.assertEqual([self.pid], self.refresh_guide())
        self.assertEqual(1, self.db.query(mdb.EPGProgram).filter_by(channel_id="gone.example").count())

    def test_a_stale_feed_cache_syncs_again(self):
        # Past the 8 h cache a sync downloads the feed anew: that can bring the
        # guide. (Asked directly: the download itself is not hermetic.)
        self.sync_epg()
        provider = self.db.get(mdb.Provider, self.pid)
        self.assertTrue(livetv_router._epg_resync_useless(self.db, provider, {"gone.example"}))
        old = datetime.now().timestamp() - xmltv.XMLTV_CACHE_MAX_AGE - 60
        os.utime(xmltv._get_cache_path(URL), (old, old))
        self.assertFalse(livetv_router._epg_resync_useless(self.db, provider, {"gone.example"}))

    def test_an_id_the_last_sync_did_not_leave_empty_syncs_again(self):
        # Programmes deleted since the sync (another provider's sync shares the id).
        self.sync_epg()
        self.db.query(mdb.EPGProgram).filter_by(channel_id="AMC.ca").delete()
        self.db.commit()
        self.assertEqual([self.pid], self.refresh_guide())


if __name__ == "__main__":
    unittest.main()
