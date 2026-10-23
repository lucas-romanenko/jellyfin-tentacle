"""A guide that is running out is synced again before the next night.

Run from the tentacle/ directory:  python -m unittest discover -s tests

The nightly job downloads the provider's XMLTV once a day. When that download
failed it "kept existing guide data" until the next night -- but on a live
install the feed reaches only a day or so ahead, and the provider
refused the nightly download on 19 of 20 nights (403) while serving the
same feed at other hours. The kept guide had already ended: every channel
showed an empty guide for days. run_guide_top_up() (hourly) syncs a guide
that ends within 12 hours again, backing off after each failure.

The provider is a local HTTP server that answers 403 or the feed; nothing
leaves the machine.
"""
import http.server
import json
import logging
import os
import shutil
import threading
import unittest
from datetime import datetime, timedelta
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb
import routers.livetv as livetv
import services.xmltv as xmltv
from tmp_dirs import temp_dir


def _stamp(dt):
    return dt.strftime("%Y%m%d%H%M%S +0000")


class FakeProvider(http.server.ThreadingHTTPServer):
    """xmltv.php: 403 while `refuse` is set, else a feed reaching 36 h ahead."""
    def __init__(self):
        super().__init__(("127.0.0.1", 0), self.Handler)
        self.refuse = True
        self.requests = 0

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            self.server.requests += 1
            if self.server.refuse:
                self.send_response(403)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            now = datetime.utcnow().replace(minute=0, second=0, microsecond=0)
            progs = "".join(
                f'<programme start="{_stamp(now + timedelta(hours=h))}" '
                f'stop="{_stamp(now + timedelta(hours=h + 1))}" channel="c.ca"><title>Show {h}</title></programme>'
                for h in range(-2, 36))
            body = f'<tv><channel id="c.ca"><display-name>CA: C</display-name></channel>{progs}</tv>'.encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/xml")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)


class GuideTopUp(unittest.TestCase):
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

        self.server = FakeProvider()
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        server = f"http://127.0.0.1:{self.server.server_address[1]}"

        p = mdb.Provider(name="P", server_url=server, username="u", password="p",
                         live_tv_enabled=True, epg_url=f"{server}/xmltv.php")
        self.db.add(p)
        self.db.commit()
        self.pid = p.id
        self.db.add(mdb.LiveChannel(provider_id=self.pid, name="CA: C", stream_id="1", epg_channel_id="c.ca",
                                    stream_url="http://192.0.2.10/live/1.ts", enabled=True))
        self.db.commit()

        self.jf_refresh = mock.Mock()
        for target, name, value in ((xmltv, "XMLTV_CACHE_DIR", os.path.join(self.tmp, "cache")),
                                    (livetv, "SessionLocal", self.Session),
                                    (livetv, "start_jellyfin_guide_refresh", self.jf_refresh)):
            pt = mock.patch.object(target, name, value)
            pt.start()
            self.addCleanup(pt.stop)
        # The three download attempts of one sync wait 30 s and 60 s between them.
        pt = mock.patch("time.sleep")
        pt.start()
        self.addCleanup(pt.stop)
        pt = mock.patch("services.provider_activity.recording_protected", lambda db: False)
        pt.start()
        self.addCleanup(pt.stop)

    def store_guide_ending(self, end):
        self.db.add(mdb.EPGProgram(channel_id="c.ca", title="Old", start=end - timedelta(hours=1), stop=end))
        self.db.commit()

    def guide_end(self):
        self.db.expire_all()
        return livetv._guide_ends_at(self.db, self.pid)

    def nightly_download(self):
        """What the nightly job runs for the provider."""
        rows = self.db.query(mdb.LiveChannel).filter(mdb.LiveChannel.provider_id == self.pid).all()
        provider = self.db.get(mdb.Provider, self.pid)
        return livetv._run_epg_sync_background(livetv._epg_provider_data(provider, rows))

    def last_attempt(self):
        """What the top-up remembers, read the way a restarted server reads it."""
        db = self.Session()
        try:
            return livetv._epg_attempt(db, self.pid)
        finally:
            db.close()

    def set_last_attempt(self, at, failures):
        mdb.set_setting(self.db, f"livetv_guide_top_up_{self.pid}",
                        json.dumps({"at": at.isoformat(), "failures": failures}))

    def age_last_attempt(self, by):
        at, failures = self.last_attempt()
        self.set_last_attempt(at - by, failures)

    def provider_health_says(self, state, step, checked_ago=timedelta(minutes=10)):
        checked = (datetime.now().astimezone() - checked_ago).isoformat()   # its clock: local, with offset
        mdb.set_setting(self.db, "provider_health", json.dumps(
            {str(self.pid): {"state": state, "step": step, "since": checked, "checked_at": checked}}))

    def test_a_refused_nightly_download_is_tried_again_hours_later(self):
        now = datetime.utcnow()
        self.store_guide_ending(now - timedelta(hours=2))      # yesterday's guide has run out

        self.assertFalse(self.nightly_download())               # 403, three attempts
        self.assertEqual(3, self.server.requests)
        self.assertLess(self.guide_end(), now, "the failed sync kept the old guide")

        livetv.run_guide_top_up()                               # the same hour: too soon
        self.assertEqual(3, self.server.requests)

        self.server.refuse = False                              # the provider serves it again
        self.age_last_attempt(timedelta(minutes=61))
        livetv.run_guide_top_up()

        self.assertEqual(4, self.server.requests)
        self.assertGreater(self.guide_end(), now + timedelta(hours=30))
        self.jf_refresh.assert_called_once()

    def test_a_guide_that_ends_within_12_hours_is_synced_again(self):
        self.store_guide_ending(datetime.utcnow() + timedelta(hours=3))
        self.server.refuse = False
        livetv.run_guide_top_up()
        self.assertEqual(1, self.server.requests)
        self.assertGreater(self.guide_end(), datetime.utcnow() + timedelta(hours=30))

    def test_a_guide_that_reaches_far_enough_is_left_alone(self):
        self.store_guide_ending(datetime.utcnow() + timedelta(hours=30))
        livetv.run_guide_top_up()
        self.assertEqual(0, self.server.requests)
        self.jf_refresh.assert_not_called()

    def test_failures_in_a_row_wait_longer_each_time(self):
        self.store_guide_ending(datetime.utcnow() - timedelta(hours=1))
        provider = self.db.get(mdb.Provider, self.pid)
        for failures, wait in ((1, 1), (2, 2), (3, 4), (4, 6), (9, 6)):
            at = datetime.utcnow()
            self.set_last_attempt(at, failures)
            self.assertIsNone(livetv.guide_top_up_due(self.db, provider, at + timedelta(hours=wait, minutes=-5)),
                              f"{failures} failure(s): not before {wait} h")
            self.assertTrue(livetv.guide_top_up_due(self.db, provider, at + timedelta(hours=wait, minutes=5)),
                            f"{failures} failure(s): due after {wait} h")

    def test_a_protected_recording_defers_it_without_counting_a_failure(self):
        self.store_guide_ending(datetime.utcnow() - timedelta(hours=1))
        with mock.patch("services.provider_activity.recording_protected", lambda db: True):
            livetv.run_guide_top_up()
        self.assertEqual(0, self.server.requests)
        self.assertIsNone(self.last_attempt())

    def test_it_waits_while_a_sync_runs(self):
        import routers.sync as sync_router
        self.store_guide_ending(datetime.utcnow() - timedelta(hours=1))
        for running, after in (({1: object()}, {}), ({}, {"nightly": datetime.utcnow()})):
            with mock.patch.object(sync_router, "_running_syncs", running), \
                 mock.patch.object(sync_router, "_after_sync", after):
                livetv.run_guide_top_up()
        self.assertEqual(0, self.server.requests)
        self.server.refuse = False
        livetv.run_guide_top_up()                     # once the sync is over
        self.assertEqual(1, self.server.requests)

    def test_a_feed_that_matches_no_channel_is_not_downloaded_again(self):
        self.set_last_attempt(datetime.utcnow() - timedelta(hours=7), 0)             # synced fine
        livetv.run_guide_top_up()                                                    # but no programmes
        self.assertEqual(0, self.server.requests)

    def test_a_provider_with_no_enabled_channel_is_skipped(self):
        self.db.query(mdb.LiveChannel).update({"enabled": False})
        self.db.commit()
        self.store_guide_ending(datetime.utcnow() - timedelta(hours=1))
        livetv.run_guide_top_up()
        self.assertEqual(0, self.server.requests)

    # ── A restart ────────────────────────────────────────────────────────────

    def test_the_back_off_is_kept_in_the_database_across_a_restart(self):
        self.store_guide_ending(datetime.utcnow() - timedelta(hours=2))
        self.assertFalse(self.nightly_download())
        at, failures = self.last_attempt()              # a new session: what a restart reads
        self.assertEqual(1, failures)
        self.assertLess(datetime.utcnow() - at, timedelta(minutes=1))
        livetv.run_guide_top_up()                       # the start-up check, minutes later
        self.assertEqual(3, self.server.requests, "a restart must not retry a refused download at once")
        self.server.refuse = False
        self.age_last_attempt(timedelta(minutes=61))
        livetv.run_guide_top_up()
        self.assertEqual(4, self.server.requests)
        self.assertEqual(0, self.last_attempt()[1])

    def test_a_deleted_providers_record_goes_with_it(self):
        self.assertIn("livetv_guide_top_up_{}", livetv.PROVIDER_SETTING_KEYS)   # deleted with the provider (#625)
        self.db.delete(self.db.get(mdb.Provider, self.pid))
        self.db.commit()
        livetv._note_epg_attempt(self.pid, False)        # a sync that ended after the delete
        self.assertIsNone(self.last_attempt(), "the next provider may get this id")

    # ── A provider that refuses ──────────────────────────────────────────────

    def test_one_download_per_top_up(self):
        self.store_guide_ending(datetime.utcnow() - timedelta(hours=1))
        livetv.run_guide_top_up()
        self.assertEqual(1, self.server.requests, "no quick retries: the hour is the retry")
        self.assertEqual(1, self.last_attempt()[1])
        self.age_last_attempt(timedelta(minutes=61))
        livetv.run_guide_top_up()
        self.assertEqual(2, self.server.requests)
        self.assertEqual(2, self.last_attempt()[1])

    def test_not_while_provider_health_finds_the_providers_server_refusing(self):
        self.store_guide_ending(datetime.utcnow() - timedelta(hours=1))
        self.server.refuse = False
        self.provider_health_says("failing", "api")
        livetv.run_guide_top_up()
        self.assertEqual(0, self.server.requests)
        self.assertIsNone(self.last_attempt(), "standing aside is no failed download")
        self.provider_health_says("ok", None)            # the hourly check: it answers again
        livetv.run_guide_top_up()
        self.assertEqual(1, self.server.requests)

    def test_a_verdict_hours_old_does_not_hold_the_guide_back(self):
        # The hourly check stood aside (live TV all evening): the provider may
        # have recovered since; one download per back-off step finds out.
        self.store_guide_ending(datetime.utcnow() - timedelta(hours=1))
        self.server.refuse = False
        self.provider_health_says("failing", "api", checked_ago=timedelta(hours=3))
        livetv.run_guide_top_up()
        self.assertEqual(1, self.server.requests)

    def test_a_refused_stream_or_a_guide_from_another_host_is_still_downloaded(self):
        self.store_guide_ending(datetime.utcnow() - timedelta(hours=1))
        self.server.refuse = False
        self.provider_health_says("failing", "stream")   # streams refused: the guide is an API download
        livetv.run_guide_top_up()
        self.assertEqual(1, self.server.requests)
        provider = self.db.get(mdb.Provider, self.pid)
        provider.server_url = "http://192.0.2.10"         # the guide comes from another host
        self.db.commit()
        self.provider_health_says("failing", "api")
        self.assertFalse(livetv._guide_server_refuses(self.db, provider))
        provider.epg_url = None                           # Xtream's own xmltv.php, on the refusing server
        self.db.commit()
        self.assertTrue(livetv._guide_server_refuses(self.db, provider))


class GuideTopUpScheduled(unittest.TestCase):
    def test_it_runs_every_hour(self):
        try:
            import main
        except Exception as e:  # pragma: no cover - depends on optional deps
            self.skipTest(f"main not importable: {e}")
        engine = create_engine("sqlite://")
        mdb.Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine)
        db = Session()
        self.addCleanup(db.close)
        with mock.patch.object(main, "schedule_once") as once, \
             mock.patch.object(main, "scheduler") as scheduler, \
             mock.patch.object(main, "SessionLocal", Session):
            main.setup_scheduler(db)
        jobs = {c.kwargs.get("id"): c for c in scheduler.add_job.call_args_list}
        self.assertIn("guide_top_up", jobs)
        job = jobs["guide_top_up"]
        self.assertIs(livetv.run_guide_top_up, job.args[0])
        self.assertEqual(timedelta(hours=1), job.args[1].interval)
        # and a few minutes after start-up: an install restarted more often
        # than hourly would otherwise never run it
        first = [c.args for c in once.call_args_list if c.args[0] is livetv.run_guide_top_up]
        self.assertEqual(1, len(first))
        self.assertTrue(60 <= first[0][1] <= 600, first[0])


if __name__ == "__main__":
    unittest.main()
