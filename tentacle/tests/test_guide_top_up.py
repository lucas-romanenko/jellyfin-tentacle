"""A failed guide download is retried until it works; a guide that is running
out is synced again before the next night (#709).

Run from the tentacle/ directory:  python -m unittest discover -s tests

The nightly job downloads the provider's XMLTV once a day. When that download
failed it "kept existing guide data" until the next night -- but on a live
install the feed reaches only a day or so ahead, and the provider
refused the nightly download on 19 of 20 nights (403) while serving the
same feed at other hours. The kept guide had already ended: every channel
showed an empty guide for days. run_guide_top_up() (every 5 minutes) tries a
failed sync again after 5, 15, 30 minutes, then hourly, syncs a guide that
ends within 12 hours again, and says a guide failing for 6 hours or run out on
the dashboard, the Activity feed and Pushover.

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
    """xmltv.php: 403 while `refuse` is set (or for the next `refuse_next`
    requests), else a feed reaching 36 h ahead. `retry_after`: the header sent
    with a 403."""
    def __init__(self):
        super().__init__(("127.0.0.1", 0), self.Handler)
        self.refuse = True
        self.refuse_next = 0
        self.retry_after = None
        self.requests = 0

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            self.server.requests += 1
            if self.server.refuse or self.server.refuse_next > 0:
                self.server.refuse_next -= 1
                self.send_response(403)
                if self.server.retry_after:
                    self.send_header("Retry-After", self.server.retry_after)
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
        self.pushover = mock.Mock(return_value=True)
        for target, name, value in (("services.pushover.send", None, self.pushover),
                                    ("routers.livetv.nightly_guide_done_at", None, None),
                                    ("routers.livetv._epg_last_failure", None, {})):
            pt = mock.patch(target, value)
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

    def set_last_attempt(self, at, failures, **more):
        mdb.set_setting(self.db, f"livetv_guide_top_up_{self.pid}",
                        json.dumps({"at": at.isoformat(), "failures": failures, **more}))

    def age_last_attempt(self, by):
        """Move the remembered sync (and when the failures began) back in time."""
        db = self.Session()
        try:
            rec = livetv._epg_record(db, self.pid)
            for key in ("at", "since", "retry_at"):
                if rec.get(key):
                    rec[key] -= by
            livetv._save_epg_record(db, self.pid, rec)
        finally:
            db.close()

    def provider_health_says(self, state, step, checked_ago=timedelta(minutes=10)):
        checked = (datetime.now().astimezone() - checked_ago).isoformat()   # its clock: local, with offset
        mdb.set_setting(self.db, "provider_health", json.dumps(
            {str(self.pid): {"state": state, "step": step, "since": checked, "checked_at": checked}}))

    def test_a_refused_nightly_download_is_tried_again_minutes_later(self):
        now = datetime.utcnow()
        self.store_guide_ending(now - timedelta(hours=2))      # yesterday's guide has run out

        self.assertFalse(self.nightly_download())               # 403, three attempts
        self.assertEqual(3, self.server.requests)
        self.assertLess(self.guide_end(), now, "the failed sync kept the old guide")

        livetv.run_guide_top_up()                               # at once: too soon
        self.assertEqual(3, self.server.requests)

        self.server.refuse = False                              # the provider serves it again
        self.age_last_attempt(timedelta(minutes=6))
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

    def test_one_channel_scheduled_weeks_ahead_does_not_hide_the_rest_running_out(self):
        # A live feed: most channels end the next morning, one runs six weeks ahead.
        now = datetime.utcnow()
        for n, (gid, end) in enumerate((("d.ca", now + timedelta(hours=3)), ("e.ca", now + timedelta(hours=4)),
                                        ("f.ca", now + timedelta(days=42))), start=2):
            self.db.add(mdb.LiveChannel(provider_id=self.pid, name=f"CA: {gid}", stream_id=str(n),
                                        epg_channel_id=gid, stream_url=f"http://192.0.2.10/live/{n}.ts", enabled=True))
            self.db.add(mdb.EPGProgram(channel_id=gid, title="Old", start=end - timedelta(hours=1), stop=end))
        self.store_guide_ending(now + timedelta(hours=2))
        self.db.commit()
        self.assertLess(self.guide_end(), now + timedelta(hours=12))
        self.server.refuse = False
        livetv.run_guide_top_up()
        self.assertEqual(1, self.server.requests)

    def test_a_guide_that_reaches_far_enough_is_left_alone(self):
        self.store_guide_ending(datetime.utcnow() + timedelta(hours=30))
        livetv.run_guide_top_up()
        self.assertEqual(0, self.server.requests)
        self.jf_refresh.assert_not_called()

    def test_failures_in_a_row_wait_longer_each_time(self):
        self.store_guide_ending(datetime.utcnow() - timedelta(hours=1))
        provider = self.db.get(mdb.Provider, self.pid)
        for failures, wait in ((1, 5), (2, 15), (3, 30), (4, 60), (9, 60), (40, 60)):
            at = datetime.utcnow()
            self.set_last_attempt(at, failures)
            self.assertIsNone(livetv.guide_top_up_due(self.db, provider, at + timedelta(minutes=wait - 2)),
                              f"{failures} failure(s): not before {wait} min")
            # a check a few seconds early still counts: the next one is 5 min away
            self.assertTrue(livetv.guide_top_up_due(self.db, provider, at + timedelta(minutes=wait, seconds=-5)),
                            f"{failures} failure(s): due after {wait} min")

    def test_a_failed_sync_is_retried_though_the_stored_guide_still_reaches_far(self):
        # Retry until one works, not only once the guide is about to run out.
        self.store_guide_ending(datetime.utcnow() + timedelta(hours=20))
        provider = self.db.get(mdb.Provider, self.pid)
        self.set_last_attempt(datetime.utcnow() - timedelta(minutes=6), 1)
        self.assertTrue(livetv.guide_top_up_due(self.db, provider))
        self.set_last_attempt(datetime.utcnow() - timedelta(minutes=6), 0)   # it worked: left alone
        self.assertIsNone(livetv.guide_top_up_due(self.db, provider))

    def test_an_empty_guide_fills_after_a_night_of_refusals(self):
        # Nothing stored any more, the nightly is refused, the provider
        # recovers after a while: 5, 15, 30 minutes, then hourly, until it works.
        now = datetime.utcnow()
        self.store_guide_ending(now - timedelta(hours=20))
        self.assertFalse(self.nightly_download())
        self.assertEqual(3, self.server.requests)
        for n, (wait, failures) in enumerate(((5, 2), (15, 3), (30, 4), (60, 5), (60, 6)), start=4):
            livetv.run_guide_top_up()
            self.assertEqual(n - 1, self.server.requests, "not before the back-off has passed")
            self.age_last_attempt(timedelta(minutes=wait, seconds=10))
            livetv.run_guide_top_up()
            self.assertEqual(n, self.server.requests, f"retried after {wait} min")
            self.assertEqual(failures, self.last_attempt()[1])
        self.server.refuse = False
        self.age_last_attempt(timedelta(minutes=61))
        livetv.run_guide_top_up()
        self.assertEqual(9, self.server.requests)
        self.assertEqual(0, self.last_attempt()[1])
        self.assertGreater(self.guide_end(), now + timedelta(hours=30))
        self.jf_refresh.assert_called_once()

    def test_the_providers_retry_after_is_honoured(self):
        self.store_guide_ending(datetime.utcnow() - timedelta(hours=1))
        self.server.retry_after = "7200"
        livetv.run_guide_top_up()
        self.assertEqual(1, self.server.requests)
        provider = self.db.get(mdb.Provider, self.pid)
        at = self.last_attempt()[0]
        self.assertIsNone(livetv.guide_top_up_due(self.db, provider, at + timedelta(minutes=118)))
        self.assertTrue(livetv.guide_top_up_due(self.db, provider, at + timedelta(minutes=121)))

    def test_a_retry_after_of_days_waits_six_hours_at_most(self):
        self.store_guide_ending(datetime.utcnow() - timedelta(hours=1))
        self.server.retry_after = str(3 * 86400)
        livetv.run_guide_top_up()
        provider = self.db.get(mdb.Provider, self.pid)
        at = self.last_attempt()[0]
        self.assertIsNone(livetv.guide_top_up_due(self.db, provider, at + timedelta(hours=5, minutes=58)))
        self.assertTrue(livetv.guide_top_up_due(self.db, provider, at + timedelta(hours=6, minutes=1)))

    def test_retry_after_as_a_date(self):
        from email.utils import format_datetime
        from datetime import timezone
        e = mock.Mock(response=mock.Mock(headers={"Retry-After": format_datetime(
            datetime.now(timezone.utc) + timedelta(minutes=40), usegmt=True)}))
        self.assertAlmostEqual(2400, livetv._retry_after_seconds(e), delta=5)
        self.assertIsNone(livetv._retry_after_seconds(mock.Mock(response=mock.Mock(headers={"Retry-After": "soon"}))))

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

    def test_it_retries_while_the_nightly_job_runs_on_after_its_guide_step(self):
        # After its guide step the nightly job asks the provider nothing more
        # (sweeps, playlists, home rows can take an hour): no reason to wait.
        import routers.sync as sync_router
        self.store_guide_ending(datetime.utcnow() - timedelta(hours=1))
        self.server.refuse = False
        started = datetime.utcnow() - timedelta(minutes=30)
        with mock.patch.object(sync_router, "_after_sync", {"nightly": started}), \
             mock.patch.object(livetv, "nightly_guide_done_at", started - timedelta(days=1)):
            livetv.run_guide_top_up()                 # last night's step: this one's is still to come
            self.assertEqual(0, self.server.requests)
        with mock.patch.object(sync_router, "_after_sync", {"nightly": started}), \
             mock.patch.object(livetv, "nightly_guide_done_at", started + timedelta(minutes=20)):
            livetv.run_guide_top_up()
        self.assertEqual(1, self.server.requests)

    def test_never_two_at_once(self):
        self.store_guide_ending(datetime.utcnow() - timedelta(hours=1))
        self.server.refuse = False
        with livetv._guide_top_up_lock:                # one runs already
            livetv.run_guide_top_up()
        self.assertEqual(0, self.server.requests)
        livetv.run_guide_top_up()
        self.assertEqual(1, self.server.requests)

    def test_a_sync_with_nothing_to_download_is_no_failure(self):
        self.db.query(mdb.LiveChannel).delete()       # "No channels yet"
        self.db.commit()
        self.assertFalse(self.nightly_download())
        self.assertEqual(0, self.server.requests)
        self.assertIsNone(self.last_attempt())

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
        livetv.run_guide_top_up()                       # the start-up check, a minute later
        self.assertEqual(3, self.server.requests, "a restart must not retry a refused download at once")
        self.server.refuse = False
        self.age_last_attempt(timedelta(minutes=6))
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
        self.assertEqual(1, self.server.requests, "no quick retries: the back-off is the retry")
        self.assertEqual(1, self.last_attempt()[1])
        self.age_last_attempt(timedelta(minutes=6))
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

    def test_retries_slow_to_hourly_while_provider_health_finds_the_server_refusing(self):
        self.store_guide_ending(datetime.utcnow() - timedelta(hours=1))
        provider = self.db.get(mdb.Provider, self.pid)
        at = datetime.utcnow()
        self.set_last_attempt(at, 1)
        self.provider_health_says("failing", "api")
        self.assertIsNone(livetv.guide_top_up_due(self.db, provider, at + timedelta(minutes=10)))
        self.assertTrue(livetv.guide_top_up_due(self.db, provider, at + timedelta(minutes=61)),
                        "still once an hour: the guide must not stay empty")

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

    # ── Telling the admin ────────────────────────────────────────────────────

    def admin_alerts_check(self):
        """The 10-minute problem check (services.admin_alerts), guides only."""
        from services import admin_alerts
        with mock.patch.object(admin_alerts, "CHECKS", (admin_alerts._seen_guides,)), \
             mock.patch("services.live_updates.publish"):
            admin_alerts.check(self.db)
        return [(n.title, n.message) for n in self.db.query(mdb.Notification).filter(
            mdb.Notification.kind == "problem").order_by(mdb.Notification.id)]

    def test_a_guide_failing_for_six_hours_is_said_once_and_its_recovery_too(self):
        self.db.add(mdb.TentacleUser(jellyfin_user_id="a" * 32, display_name="Lucas", is_admin=True))
        self.db.commit()
        self.store_guide_ending(datetime.utcnow() + timedelta(hours=20))
        self.assertFalse(self.nightly_download())         # fails: 403
        self.assertEqual(1, self.last_attempt()[1])
        provider = self.db.get(mdb.Provider, self.pid)
        self.assertIsNone(livetv.guide_problem(self.db, provider), "a fresh failure is no alarm yet")
        self.assertEqual([], self.admin_alerts_check())

        self.age_last_attempt(timedelta(hours=6, minutes=1))
        livetv.run_guide_top_up()                         # fails again, 6 h after the first
        problem = livetv.guide_problem(self.db, provider)
        self.assertIn("isn't downloading", problem)
        self.assertIn("2 download(s) failed", problem)
        self.assertIn("HTTP 403", problem)
        self.assertNotIn("127.0.0.1", problem)
        notes = self.admin_alerts_check()
        self.assertEqual(1, len(notes))
        self.assertEqual("The Live TV guide of P isn't updating", notes[0][0])
        self.assertEqual(problem, notes[0][1])
        self.assertEqual(1, self.pushover.call_count)
        from services import admin_alerts
        self.assertEqual([{"area": "guide", "text": "The TV guide may be missing programmes right now. "
                                                    "Lucas knows."}], admin_alerts.family_messages(self.db))

        self.age_last_attempt(timedelta(minutes=16))
        livetv.run_guide_top_up()                         # fails again: no second message
        self.assertEqual(1, len(self.admin_alerts_check()))
        self.assertEqual(1, self.pushover.call_count)

        from services import provider_health
        view = {p["id"]: p for p in provider_health.provider_view(self.db)}
        self.assertIn("isn't downloading", view[self.pid]["guide_problem"])   # the dashboard banner

        self.server.refuse = False
        self.age_last_attempt(timedelta(minutes=31))
        livetv.run_guide_top_up()                         # works
        self.assertIsNone(livetv.guide_problem(self.db, provider))
        notes = self.admin_alerts_check()
        self.assertEqual("Fixed: The Live TV guide of P isn't updating", notes[-1][0])
        self.assertEqual(2, self.pushover.call_count)
        view = {p["id"]: p for p in provider_health.provider_view(self.db)}
        self.assertIsNone(view[self.pid]["guide_problem"])

    def test_a_guide_that_has_run_out_is_said_at_once(self):
        self.store_guide_ending(datetime.utcnow() - timedelta(hours=1))
        livetv.run_guide_top_up()                         # refused
        provider = self.db.get(mdb.Provider, self.pid)
        problem = livetv.guide_problem(self.db, provider)
        self.assertIn("ran out", problem)
        self.assertIn("Tentacle keeps trying", problem)
        self.admin_alerts_check()
        self.pushover.assert_called_once()

    def test_a_guide_that_ran_out_with_no_failed_download_is_said_too(self):
        # e.g. the nightly download stood aside for a recording all night
        self.store_guide_ending(datetime.utcnow() - timedelta(minutes=10))
        provider = self.db.get(mdb.Provider, self.pid)
        problem = livetv.guide_problem(self.db, provider)
        self.assertTrue(problem.startswith("The Live TV guide ran out at "), problem)
        self.assertNotIn("keeps trying", problem)


class GuideTopUpScheduled(unittest.TestCase):
    def test_it_runs_every_five_minutes(self):
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
        self.assertEqual(timedelta(minutes=5), job.args[1].interval)
        self.assertEqual(0, len([c for c in once.call_args_list if c.args[0] is livetv.run_guide_top_up]),
                         "no separate start-up check: it ran at the same moment as the first tick")


if __name__ == "__main__":
    unittest.main()
