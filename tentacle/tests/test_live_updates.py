"""Live updates (services/live_updates.py): a change reaches the open screens
of the people it concerns within a second or two, without a reload.

Lucas, 2026-10-22: he approved a request while the person had Tentacle open;
their Activity badge didn't show "1" until they clicked. Every change must
reach open screens within ~10 s, ideally 1-2 s.

Run from the tentacle/ directory:  python -m unittest tests.test_live_updates
"""
import asyncio
import json
import threading
import unittest
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb
from services import approvals, live_updates as LU
from tmp_dirs import temp_dir

ADMIN, ANN, BEN, ADMIN2 = "a" * 32, "b" * 32, "c" * 32, "e" * 32


class Hub(unittest.TestCase):
    """Who hears what."""

    def setUp(self):
        LU.reset()
        self.addCleanup(LU.reset)

    def _listen(self, user_id, is_admin, notes, ready, stop):
        async def run():
            gen = LU.stream(user_id, is_admin)
            hello = await gen.__anext__()
            notes.append(("hello", hello))
            ready.set()
            while not stop.is_set():
                try:
                    chunk = await asyncio.wait_for(gen.__anext__(), timeout=0.05)
                except asyncio.TimeoutError:
                    continue
                if chunk.startswith("event: change"):
                    notes.append(("change", json.loads(chunk.split("data: ", 1)[1])))
            await gen.aclose()
        asyncio.run(run())

    def _tabs(self, *people):
        """Open tabs for (user_id, is_admin); returns their note lists and a closer."""
        stop = threading.Event()
        lists, threads = [], []
        for uid, admin in people:
            notes, ready = [], threading.Event()
            t = threading.Thread(target=self._listen, args=(uid, admin, notes, ready, stop), daemon=True)
            t.start()
            self.assertTrue(ready.wait(5))
            lists.append(notes)
            threads.append(t)

        def close():
            stop.set()
            for t in threads:
                t.join(5)
        self.addCleanup(close)
        return lists, close

    def _topics(self, notes):
        return [n["topic"] for kind, n in notes if kind == "change"]

    def _wait(self, cond):
        for _ in range(100):
            if cond():
                return
            threading.Event().wait(0.02)
        self.fail("no note arrived")

    def test_hello_then_notes_for_the_right_people(self):
        (admin, ann, ben), close = self._tabs((1, True), (2, False), (3, False))
        self.assertEqual(3, LU.subscribers())
        for notes in (admin, ann, ben):
            self.assertTrue(notes[0][1].startswith("retry: 3000\nevent: hello\n"))
        LU.publish("requests", user_ids=[2], admins=True)
        LU.publish("library", everyone=True, data={"event": "movie_added", "tmdb_id": 1})
        LU.publish("me", user_ids=[3])
        self._wait(lambda: len(self._topics(ben)) == 2 and len(self._topics(admin)) == 2)
        self._wait(lambda: len(self._topics(ann)) == 2)
        self.assertEqual(["requests", "library"], self._topics(admin))
        self.assertEqual(["requests", "library"], self._topics(ann))
        self.assertEqual(["library", "me"], self._topics(ben))
        lib = [n for kind, n in ann if kind == "change" and n["topic"] == "library"][0]
        self.assertEqual({"event": "movie_added", "tmdb_id": 1}, lib["data"])
        close()
        self.assertEqual(0, LU.subscribers())

    def test_stopping_the_server_ends_every_channel(self):
        (ann, admin), close = self._tabs((2, False), (1, True))
        LU.close_all()
        self._wait(lambda: LU.subscribers() == 0)

    def test_publishing_from_any_thread_never_raises(self):
        self.assertEqual(0, LU.publish("requests", user_ids=["x"], admins=True))
        (ann,), _ = self._tabs((2, False))
        t = threading.Thread(target=LU.publish, args=("activity",), kwargs={"user_ids": [2]})
        t.start()
        t.join()
        self._wait(lambda: self._topics(ann) == ["activity"])

    def test_a_stuck_tab_gets_one_resync_not_a_backlog(self):
        loop = asyncio.new_event_loop()
        self.addCleanup(loop.close)
        sub = LU._Sub(1, False, loop)
        for i in range(LU.QUEUE_SIZE + 5):
            sub.offer({"topic": "activity", "seq": i})
        self.assertEqual(1, sub.queue.qsize())
        self.assertEqual({"topic": "resync"}, sub.queue.get_nowait())


class Clients(unittest.TestCase):
    """Jellyfin Web and the TV app hear through the plugin's /Tentacle/Notify."""

    def setUp(self):
        LU.reset()
        self.addCleanup(LU.reset)
        self.tmp = temp_dir(self)
        engine = create_engine(f"sqlite:///{self.tmp}/t.db")
        self.addCleanup(engine.dispose)
        mdb.Base.metadata.create_all(engine)
        self.Session = sessionmaker(bind=engine)
        db = self.Session()
        db.add_all([mdb.TentacleUser(jellyfin_user_id=ADMIN, display_name="Lucas", is_admin=True),
                    mdb.TentacleUser(jellyfin_user_id=ANN, display_name="Ann", is_admin=False),
                    mdb.TentacleUser(jellyfin_user_id=BEN, display_name="Ben", is_admin=False)])
        mdb.set_setting(db, "jellyfin_url", "http://jf:8096/")
        mdb.set_setting(db, "jellyfin_api_key", "k" * 32)
        db.commit()
        db.close()
        p = mock.patch("models.database.SessionLocal", self.Session)
        p.start()
        self.addCleanup(p.stop)

    def test_nothing_is_sent_unless_the_app_runs(self):
        with mock.patch.object(LU, "post_notify") as post:
            LU.tell_clients("requests", user_ids=[2], admins=True)
            self.assertIsNone(LU._client_timer)
        post.assert_not_called()

    def test_a_burst_is_one_call_per_set_of_topics(self):
        LU._clients_on = True
        with mock.patch("requests.post") as post:
            post.return_value.status_code = 200
            LU.tell_clients("requests", user_ids=[2], admins=True)
            LU.tell_clients("notifications", user_ids=[2])
            LU.tell_clients("activity", user_ids=[2], admins=True)
            LU.tell_clients("people", admins=True)          # not a client topic
            LU._client_timer.cancel()
            LU._send_to_clients()
        calls = sorted((c.kwargs["params"]["topics"], c.kwargs["params"]["userIds"]) for c in post.call_args_list)
        self.assertEqual([("activity,notifications,requests", ANN), ("activity,requests", ADMIN)], calls)
        self.assertEqual("http://jf:8096/Tentacle/Notify", post.call_args.args[0])
        self.assertEqual({"X-Emby-Token": "k" * 32}, post.call_args.kwargs["headers"])


class Requests(unittest.TestCase):
    """Every step of a request is told to the admins and to the person."""

    def setUp(self):
        LU.reset()
        self.addCleanup(LU.reset)
        self.tmp = temp_dir(self)
        engine = create_engine(f"sqlite:///{self.tmp}/t.db")
        self.addCleanup(engine.dispose)
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)
        self.admin = mdb.TentacleUser(jellyfin_user_id=ADMIN, display_name="Lucas", is_admin=True)
        self.ann = mdb.TentacleUser(jellyfin_user_id=ANN, display_name="Ann", is_admin=False)
        self.db.add_all([self.admin, self.ann])
        self.db.commit()
        for target, kw in (("services.approvals._details", {"return_value": {"title": "Dune"}}),
                           ("services.pushover.send", {"return_value": False})):
            p = mock.patch(target, **kw)
            p.start()
            self.addCleanup(p.stop)
        self.told = []
        p = mock.patch.object(LU, "changed", side_effect=lambda topic, **kw: self.told.append(
            (topic, tuple(kw.get("user_ids") or ()), kw.get("admins", False), kw.get("clients", False))))
        p.start()
        self.addCleanup(p.stop)

    def _requests_told(self):
        return [t for t in self.told if t[0] == "requests"]

    def test_asking_tells_the_admins_and_the_person(self):
        approvals.hold(self.db, self.ann, "movie", {"tmdb_ids": [1]}, tmdb_ids=[1])
        self.assertIn(("requests", (self.ann.id,), True, True), self.told)
        # The admin's "Ann asked for this" message is a notification for the admin.
        self.assertIn(("notifications", (self.admin.id,), False, True), self.told)

    def test_approving_tells_requests_and_activity(self):
        approvals.hold(self.db, self.ann, "movie", {"tmdb_ids": [1]}, tmdb_ids=[1])
        self.told.clear()
        outcome = mock.Mock()
        outcome.as_response.return_value = {"added": 1}
        with mock.patch("services.media_requests.request_movies", return_value=outcome), \
                mock.patch("routers.activity.invalidate_wanted_cache") as inv:
            approvals.approve(self.db, self.db.query(mdb.MediaRequest).one().id, self.admin)
        inv.assert_called_once_with()      # it shows as searching at once
        self.assertIn(("requests", (self.ann.id,), True, True), self.told)
        self.assertIn(("activity", (self.ann.id,), True, True), self.told)
        self.assertIn(("notifications", (self.ann.id,), False, True), self.told)

    def test_declining_and_taking_back(self):
        approvals.hold(self.db, self.ann, "movie", {"tmdb_ids": [1]}, tmdb_ids=[1])
        row = self.db.query(mdb.MediaRequest).one()
        self.told.clear()
        approvals.decline(self.db, row.id, self.admin, "Not tonight")
        self.assertEqual([("requests", (self.ann.id,), True, True)], self._requests_told())
        approvals.hold(self.db, self.ann, "movie", {"tmdb_ids": [2]}, tmdb_ids=[2])
        row = self.db.query(mdb.MediaRequest).filter(mdb.MediaRequest.status == "pending").one()
        self.told.clear()
        self.assertTrue(approvals.cancel(self.db, row.id, self.ann))
        self.assertEqual([("requests", (self.ann.id,), True, True)], self._requests_told())

    def test_dismissing_a_message_reaches_the_other_screens(self):
        from routers import notifications
        from routers.auth import get_user_from_request
        mdb.create_notification(self.db, self.ann.id, 1, "movie", "Up", "Up is ready to watch")
        n = self.db.query(mdb.Notification).one()
        app = FastAPI()
        app.include_router(notifications.router)
        app.dependency_overrides[mdb.get_db] = lambda: self.db
        app.dependency_overrides[get_user_from_request] = lambda: self.ann
        self.told.clear()
        self.assertEqual(200, TestClient(app).post(f"/api/notifications/{n.id}/dismiss").status_code)
        self.assertEqual([("notifications", (self.ann.id,), False, True)], self.told)


class OthersDownloads(unittest.TestCase):
    """Admins choose whether other people's downloads are in their Activity
    (default on), and each one says whose it is."""

    def setUp(self):
        LU.reset()
        self.addCleanup(LU.reset)
        self.tmp = temp_dir(self)
        engine = create_engine(f"sqlite:///{self.tmp}/t.db")
        self.addCleanup(engine.dispose)
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)
        self.admin = mdb.TentacleUser(jellyfin_user_id=ADMIN, display_name="Lucas", is_admin=True)
        self.admin2 = mdb.TentacleUser(jellyfin_user_id=ADMIN2, display_name="Dana", is_admin=True)
        self.ann = mdb.TentacleUser(jellyfin_user_id=ANN, display_name="Ann", is_admin=False)
        self.db.add_all([self.admin, self.admin2, self.ann])
        self.db.commit()
        self.db.add_all([mdb.DownloadRequest(tmdb_id=1, media_type="movie", user_id=self.ann.id),
                         mdb.DownloadRequest(tmdb_id=2, media_type="movie", user_id=self.admin.id)])
        self.db.commit()

    def test_default_on_for_admins_only(self):
        self.assertTrue(mdb.sees_others_downloads(self.admin))
        self.assertFalse(mdb.sees_others_downloads(self.ann))
        self.admin.show_others_downloads = None     # a row from before the setting
        self.assertTrue(mdb.sees_others_downloads(self.admin))
        self.admin.show_others_downloads = False
        self.assertFalse(mdb.sees_others_downloads(self.admin))

    def _activity(self, user):
        from routers import activity
        downloads = [{"tmdb_id": t, "title": f"T{t}", "media_type": "movie", "source": "radarr",
                      "status": "downloading", "progress": 50.0, "queue_id": t} for t in (1, 2, 3)]
        wanted = {"unreleased": [], "searching": [{"tmdb_id": 4, "media_type": "movie", "title": "T4"}],
                  "_unreleased_all": [], "_searching_all": [{"tmdb_id": 4, "media_type": "movie", "title": "T4"}]}
        self.db.add(mdb.DownloadRequest(tmdb_id=4, media_type="movie", user_id=self.ann.id)) \
            if not self.db.query(mdb.DownloadRequest).filter_by(tmdb_id=4).first() else None
        self.db.commit()
        with mock.patch.object(activity, "_build_downloads", return_value=downloads), \
                mock.patch.object(activity, "_get_wanted", return_value=wanted), \
                mock.patch.object(activity, "_watch_arr_searches"), \
                mock.patch.object(activity, "_get_recently_downloaded", return_value=[]), \
                mock.patch("services.arr_insight.searching_problems", return_value=[]), \
                mock.patch("services.arr_insight.coming_up", return_value=[]), \
                mock.patch("services.arr_insight.cached_line", return_value=None), \
                mock.patch("services.download_readiness.in_flight", return_value=[]):
            return activity.get_activity(mock.Mock(), self.db, user)

    def test_an_admin_sees_everyones_marked_whose_it_is(self):
        out = self._activity(self.admin)
        self.assertTrue(out["for_names"])
        self.assertEqual([(1, "Ann"), (2, None), (3, None)],
                         [(d["tmdb_id"], d.get("for")) for d in out["downloads"]])
        self.assertEqual([(4, "Ann")], [(x["tmdb_id"], x.get("for")) for x in out["searching"]])
        # requested_by stays for clients from before "for" (their own name on their own).
        self.assertEqual(["Ann", "Lucas", None], [d["requested_by"] for d in out["downloads"]])

    def test_turned_off_only_their_own_and_nobodys(self):
        self.admin.show_others_downloads = False
        self.db.commit()
        out = self._activity(self.admin)
        self.assertEqual([2, 3], [d["tmdb_id"] for d in out["downloads"]])
        self.assertEqual([], out["searching"])
        self.assertNotIn("for", out["downloads"][0])

    def test_family_only_ever_see_their_own(self):
        out = self._activity(self.ann)
        self.assertEqual([1], [d["tmdb_id"] for d in out["downloads"]])
        self.assertNotIn("for", out["downloads"][0])
        self.assertNotIn("requested_by", out["downloads"][0])

    def test_ready_to_watch_reaches_the_admins_who_follow_it(self):
        self.admin2.show_others_downloads = False
        self.db.commit()
        mdb.tell_admins_ready(self.db, self.ann.id, 1, "movie", "Up", "Up is ready to watch", "/p.jpg", "jf1")
        rows = self.db.query(mdb.Notification).all()
        self.assertEqual([(self.admin.id, self.ann.id, "Up is ready to watch")],
                         [(n.user_id, n.for_user_id, n.message) for n in rows])
        # The admin's own download: nobody else is told about it as "theirs".
        mdb.tell_admins_ready(self.db, self.admin.id, 2, "movie", "Mine", "Mine is ready", already={self.admin.id})
        self.assertEqual(1, self.db.query(mdb.Notification).count())

    def test_notifications_say_whose_and_the_setting_switches(self):
        from routers import notifications
        from routers.auth import get_user_from_request
        mdb.tell_admins_ready(self.db, self.ann.id, 1, "movie", "Up", "Up is ready to watch")
        mdb.create_notification(self.db, self.admin.id, 2, "movie", "Mine", "Mine is ready")
        app = FastAPI()
        app.include_router(notifications.router)
        app.dependency_overrides[mdb.get_db] = lambda: self.db
        who = {"user": self.admin}
        app.dependency_overrides[get_user_from_request] = lambda: who["user"]
        c = TestClient(app)
        got = c.get("/api/notifications").json()
        self.assertTrue(got["show_others_downloads"])
        self.assertEqual({"Up is ready to watch": "Ann", "Mine is ready": None},
                         {n["message"]: n.get("for") for n in got["notifications"]})
        with mock.patch.object(LU, "publish") as pub:
            self.assertEqual({"show_others_downloads": False}, c.put("/api/notifications/others", json={"show": False}).json())
        self.assertFalse(self.admin.show_others_downloads)
        self.assertIn(mock.call("activity", user_ids=[self.admin.id]), pub.call_args_list)
        who["user"] = self.ann
        self.assertNotIn("show_others_downloads", c.get("/api/notifications").json())
        self.assertEqual(403, c.put("/api/notifications/others", json={"show": True}).status_code)


class ActivityActions(unittest.TestCase):
    def test_removing_or_stopping_a_title_reaches_every_open_activity(self):
        from routers import activity
        LU.reset()
        self.addCleanup(LU.reset)
        title = activity.ArrTitle(media_type="movie", tmdb_id=5)
        with mock.patch.object(activity, "invalidate_wanted_cache"), \
                mock.patch("services.arr_insight.forget"), \
                mock.patch("routers.discover.bust_arr_ids_cache"), \
                mock.patch.object(LU, "publish") as pub, mock.patch.object(LU, "tell_clients") as tell:
            activity._after_arr_change(title)
        pub.assert_called_once_with("activity", everyone=True)
        tell.assert_called_once_with("activity", user_ids=(), admins=True)


class ActivityWatch(unittest.TestCase):
    """Radarr/Sonarr say nothing when a download starts or moves on: Tentacle
    reads their queues and tells who it concerns."""

    def setUp(self):
        LU.reset()
        self.addCleanup(LU.reset)
        self.w = LU._ActivityWatch()

    def _snap(self, *items):
        return {("radarr", i): {"media_type": "movie", "tmdb_id": t, "state": st, "progress": pr}
                for i, t, st, pr in items}

    def test_came_went_or_changed_state_is_told_progress_only_to_dashboards(self):
        snaps = [self._snap((1, 10, ("downloading",), 5)),
                 self._snap((1, 10, ("downloading",), 5)),
                 self._snap((1, 10, ("downloading",), 6)),
                 self._snap((1, 10, ("completed",), 100)),
                 self._snap()]
        with mock.patch.object(LU, "snapshot", side_effect=snaps), \
                mock.patch.object(LU, "owners_of", return_value={7}), \
                mock.patch.object(LU, "changed") as changed, mock.patch.object(LU, "publish") as publish:
            self.w.tick()          # the first look: nothing to compare with
            self.w.tick()          # nothing moved
            changed.assert_not_called(); publish.assert_not_called()
            self.w.tick()          # 5% -> 6%
            publish.assert_called_once_with("activity", user_ids={7}, admins=True, data={"progress": True})
            changed.assert_not_called()
            self.w.tick()          # done downloading
            self.w.tick()          # gone from the queue
        self.assertEqual([mock.call("activity", user_ids={7}, admins=True, clients=True)] * 2,
                         changed.call_args_list)

    def test_a_queue_that_doesnt_answer_is_no_news(self):
        with mock.patch.object(LU, "snapshot", side_effect=[self._snap((1, 10, ("downloading",), 5)), None,
                                                            self._snap((1, 10, ("downloading",), 5))]), \
                mock.patch.object(LU, "changed") as changed:
            self.w.tick(); self.w.tick(); self.w.tick()
        changed.assert_not_called()


if __name__ == "__main__":
    unittest.main()
