"""Unfollowing a show only stops new seasons; it never re-monitors the show.

Follow / Unfollow (dashboard, plugin, TV app: POST /api/library/follow/<id>)
calls SonarrService.set_follow, which wrote monitorNewItems AND set the
series' `monitored` to True for both. On a show that is unmonitored in
Sonarr (unmonitored there by hand, or by Tentacle itself after a custom
episode pick without new episodes) an Unfollow turned Sonarr's monitoring
back on, so Sonarr's RSS sync went on to grab every monitored-but-missing
episode of it: the opposite of what Unfollow asks for.

Following still has to turn the show's monitoring on (Sonarr grabs nothing
for an unmonitored series); unfollowing leaves it as it is.
"""
import unittest
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb
from services.sonarr import SonarrService
from tmp_dirs import temp_dir


class FakeSonarr:
    """GET /api/v3/series and PUT /api/v3/series/<id>, as requests sees them."""

    def __init__(self, series):
        self.series = [dict(s) for s in series]
        self.puts = []

    def get(self, url, params=None, timeout=None, **kw):
        return mock.Mock(status_code=200, json=lambda: [dict(s) for s in self.series],
                         raise_for_status=lambda: None)

    def put(self, url, json=None, timeout=None, **kw):
        self.puts.append(dict(json))
        for s in self.series:
            if f"/api/v3/series/{s['id']}" in url:
                s.update(json)
        return mock.Mock(status_code=202)


def show(monitored, monitor_new):
    return {"id": 9, "tmdbId": 321, "tvdbId": 654, "title": "A show", "monitored": monitored,
            "monitorNewItems": monitor_new, "seasons": [{"seasonNumber": 1, "monitored": True}]}


class SetFollow(unittest.TestCase):
    def follow(self, series, follow):
        sonarr = SonarrService("http://sonarr.test", "k")
        fake = FakeSonarr([series])
        sonarr.session = fake
        self.assertTrue(sonarr.set_follow(321, follow))
        self.assertEqual(1, len(fake.puts))
        return fake.puts[0]

    def test_unfollow_an_unmonitored_show_leaves_it_unmonitored(self):
        sent = self.follow(show(monitored=False, monitor_new="all"), follow=False)
        self.assertEqual("none", sent["monitorNewItems"])
        self.assertIs(False, sent["monitored"], "Unfollow turned the show's Sonarr monitoring on")

    def test_unfollow_a_monitored_show_keeps_it_monitored(self):
        # Wanted episodes still come: Unfollow is about new seasons only.
        sent = self.follow(show(monitored=True, monitor_new="all"), follow=False)
        self.assertEqual("none", sent["monitorNewItems"])
        self.assertIs(True, sent["monitored"])

    def test_follow_an_unmonitored_show_monitors_it(self):
        # Sonarr grabs nothing for an unmonitored series, so following has to
        # turn it on.
        sent = self.follow(show(monitored=False, monitor_new="none"), follow=True)
        self.assertEqual("all", sent["monitorNewItems"])
        self.assertIs(True, sent["monitored"])

    def test_the_rest_of_the_series_goes_back_as_sonarr_sent_it(self):
        sent = self.follow(show(monitored=False, monitor_new="all"), follow=False)
        self.assertEqual([{"seasonNumber": 1, "monitored": True}], sent["seasons"])
        self.assertEqual((9, 654, "A show"), (sent["id"], sent["tvdbId"], sent["title"]))


class FollowRoute(unittest.TestCase):
    """The route the dashboard, the plugin and the TV app call."""

    def setUp(self):
        tmp = temp_dir(self)
        engine = create_engine(f"sqlite:///{tmp}/t.db", connect_args={"check_same_thread": False})
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)
        mdb.set_setting(self.db, "sonarr_url", "http://sonarr.test")
        mdb.set_setting(self.db, "sonarr_api_key", "k")
        self.admin = mdb.TentacleUser(jellyfin_user_id="a" * 32, display_name="Admin", is_admin=True)
        self.db.add(self.admin)
        self.db.add(mdb.Series(tmdb_id=321, title="A show", source="sonarr", sonarr_monitored=True))
        self.db.commit()

    def test_unfollow_keeps_sonarr_monitoring_off(self):
        fake = FakeSonarr([show(monitored=False, monitor_new="all")])
        real_init = SonarrService.__init__

        def init(svc, url, key):
            real_init(svc, url, key)
            svc.session = fake

        from routers.library import FollowBody, toggle_follow
        with mock.patch.object(SonarrService, "__init__", init):
            out = toggle_follow(321, FollowBody(follow=False), db=self.db, user=self.admin)
        self.assertEqual({"success": True, "following": False}, out)
        self.assertEqual([("none", False)], [(p["monitorNewItems"], p["monitored"]) for p in fake.puts])
        self.assertFalse(self.db.query(mdb.Series).filter_by(tmdb_id=321).one().sonarr_monitored)


if __name__ == "__main__":
    unittest.main()
