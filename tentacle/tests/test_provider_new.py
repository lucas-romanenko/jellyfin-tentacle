"""New from your provider (services/provider_new.py): groups and categories
a provider added wait for the admin's review; step 1 ticks or dismisses
groups, step 2 picks their channels into the pool; the pool marks what is
new for people; the checker tries new groups' channels first.

Run from the tentacle/ directory:  python -m unittest tests.test_provider_new
"""
import json
import logging
import unittest
from datetime import datetime, timedelta
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb
from services import livetv_catalog as C
from services import livetv_checker as K
from services import provider_new as N
from tmp_dirs import temp_dir


class Case(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        engine = create_engine(f"sqlite:///{temp_dir(self)}/t.db", connect_args={"check_same_thread": False})
        self.addCleanup(engine.dispose)
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)
        self.p = mdb.Provider(name="Bears", server_url="http://192.0.2.10", username="u", password="p",
                              live_tv_enabled=True, active=True)
        self.db.add(self.p)
        self.db.commit()
        C._invalidate()
        self.addCleanup(C._invalidate)
        K._new_cache.update(at=-1e9, ids=[])
        K._pool_cache.update(at=-1e9, ids=[])
        self.addCleanup(K._new_cache.update, at=-1e9, ids=[])
        self.addCleanup(K._pool_cache.update, at=-1e9, ids=[])
        for target in ("services.livetv_tuners.schedule_sync",):
            patcher = mock.patch(target)
            patcher.start()
            self.addCleanup(patcher.stop)

    def sync_groups(self, cats):
        from routers.livetv import _sync_groups
        _sync_groups(self.p.id, [{"category_id": c, "category_name": n} for c, n in cats], self.db,
                     {c: 3 for c, _ in cats})
        self.db.commit()

    def group(self, name):
        return self.db.query(mdb.LiveChannelGroup).filter_by(name=name).one()

    def channel(self, sid, name, group, enabled=False, gid=None, state=None):
        c = mdb.LiveChannel(provider_id=self.p.id, name=name, stream_id=str(sid),
                            stream_url=f"http://192.0.2.10/live/u/p/{sid}.ts", group_title=group,
                            epg_channel_id=gid, enabled=enabled, check_state=state,
                            check_at=datetime.utcnow() if state else None)
        self.db.add(c)
        self.db.commit()
        return c


class Arrival(Case):
    def test_a_providers_first_groups_are_its_setup_later_ones_are_new(self):
        self.sync_groups([("1", "CA| SPORTS"), ("2", "US| NEWS")])
        self.assertIsNone(N.review_of(self.group("CA| SPORTS")))
        self.assertEqual({"live": 0, "vod": 0}, N.counts(self.db))
        self.sync_groups([("1", "CA| SPORTS"), ("2", "US| NEWS"), ("3", "UK| PPV")])
        g = self.group("UK| PPV")
        self.assertEqual("new", N.review_of(g))
        self.assertIsNotNone(g.first_seen_at)
        self.assertEqual({"live": 1, "vod": 0}, N.counts(self.db))

    def test_vod_categories_from_a_refresh_are_new(self):
        from routers.providers import fetch_categories
        cats = [[{"category_id": "1", "category_name": "Netflix"}], [], {"1": 5}, {}]
        with mock.patch("routers.providers.fetch_provider_categories", lambda p: cats), \
                mock.patch("services.provider_activity.refuse_while_recording", lambda db, what: None):
            fetch_categories(self.p.id, self.db)
            cats[0] = cats[0] + [{"category_id": "2", "category_name": "Apple TV+"}]
            fetch_categories(self.p.id, self.db)
        new = N.vod_overview(self.db)["categories"]
        self.assertEqual(["Apple TV+"], [c["name"] for c in new])
        self.assertEqual(1, N.counts(self.db)["vod"])


class FromTheOldNotice(Case):
    def test_groups_since_the_first_one_the_notice_named_are_new_with_their_dates(self):
        self.sync_groups([(str(i), f"OLD {i}") for i in range(3)])
        self.sync_groups([(str(i), f"OLD {i}") for i in range(3)] + [("10", "A"), ("11", "B"), ("12", "C"), ("13", "D")])
        for name in "ABCD":   # as they were before reviews existed
            self.group(name).review_state = self.group(name).first_seen_at = None
        self.group("C").enabled = True    # chosen already
        self.db.query(mdb.Setting).filter_by(key=N._MIGRATED_KEY).delete()
        mdb.set_setting(self.db, "new_content_notice", json.dumps(
            {"live": ["A"], "live_total": 4, "detected_at": "2026-09-27T07:22:33.07+00:00"}))
        self.db.add(mdb.ActivityLog(event="new_live_groups", message="2 new Live TV groups from provider: A, B",
                                    created_at=datetime(2026, 8, 4, 7, 40)))
        self.db.add(mdb.ActivityLog(event="new_live_groups", message="1 new Live TV group from provider: D",
                                    created_at=datetime(2026, 9, 1, 7, 40)))
        self.db.commit()
        N.ensure_migrated(self.db)
        got = {g.name: (N.review_of(g), N._iso(g.first_seen_at)) for g in self.db.query(mdb.LiveChannelGroup)}
        self.assertEqual(("new", "2026-08-04T07:40:00Z"), got["A"])
        self.assertEqual(("new", "2026-08-04T07:40:00Z"), got["B"])
        self.assertIsNone(got["C"][0])
        self.assertEqual(("new", "2026-09-01T07:40:00Z"), got["D"])
        self.assertIsNone(got["OLD 1"][0])
        # Once only.
        self.group("A").review_state = None
        self.db.commit()
        N.ensure_migrated(self.db)
        self.assertIsNone(N.review_of(self.group("A")))


class Review(Case):
    def setUp(self):
        super().setUp()
        self.sync_groups([("1", "CA| SPORTS")])
        self.sync_groups([("1", "CA| SPORTS"), ("2", "US| AT&T"), ("3", "UK| PPV")])
        self.group("CA| SPORTS").enabled = True
        self.db.commit()
        self.tsn = self.channel(1, "CA EN: TSN 1", "CA| SPORTS", enabled=True, gid="TSN1.ca")
        # The new group: TSN 1 again (in the pool already), HGTV in two
        # qualities (one network), CNN, a separator.
        self.c_tsn = self.channel(10, "US: TSN 1 HD", "US| AT&T", state="works")
        self.h1 = self.channel(11, "US: HGTV HD", "US| AT&T", state="broken", gid="HGTV.us")
        self.h2 = self.channel(12, "TV: HGTV WEST", "US| AT&T", state="works")
        self.cnn = self.channel(13, "US: CNN", "US| AT&T")
        self.channel(14, "##### NEWS #####", "US| AT&T")
        # The PPV group: an event and an empty event slot.
        self.ev = self.channel(20, "EPL 01 | 3:00pm Arsenal vs Chelsea", "UK| PPV")
        self.slot = self.channel(21, "EPL 02 |", "UK| PPV")
        now = datetime.utcnow()
        self.db.add(mdb.EPGProgram(channel_id="HGTV.us", title="Show", start=now, stop=now + timedelta(hours=1)))
        self.db.commit()
        for g in (self.group("US| AT&T"), self.group("UK| PPV")):
            g.channels_read_at = N._now()
        self.db.commit()

    def test_the_list_counts_channels_after_folding_with_works_and_guide(self):
        with mock.patch.object(N, "read_new_in_background") as bg:
            out = N.live_overview(self.db)
        bg.assert_not_called()
        groups = {g["name"]: g for g in out["groups"]}
        self.assertEqual({"US| AT&T", "UK| PPV"}, set(groups))
        att = groups["US| AT&T"]
        self.assertEqual(3, att["channels"])        # TSN, HGTV (2 feeds), CNN; no separator
        self.assertEqual(2, att["works"])           # TSN, HGTV (its West feed works)
        self.assertEqual(1, att["with_guide"])      # HGTV
        self.assertEqual("Bears", att["provider"])
        ppv = groups["UK| PPV"]
        self.assertEqual((2, 2, 1), (ppv["channels"], ppv["events"], ppv["no_event"]))

    def test_the_household_is_its_channels_places_its_region_and_the_browser(self):
        home = N.household(self.db, country="us", lang="en-CA")
        self.assertEqual("CA", home["countries"][0])     # the pool: CA| SPORTS
        self.assertIn("US", home["countries"])           # the browser
        self.assertEqual(["en"], home["languages"])

    def test_groups_from_the_households_places_and_languages_are_probably_for_it(self):
        self.sync_groups([("1", "CA| SPORTS"), ("2", "US| AT&T"), ("3", "UK| PPV"), ("4", "ES| F1 PPV ᴿᴬᵂ"),
                          ("5", "CA| CINEMA FR")])
        with mock.patch.object(N, "read_new_in_background"), \
                mock.patch("services.livetv_catalog._household", return_value=(None, "en")):
            groups = {g["name"]: g for g in N.live_overview(self.db)["groups"]}
            self.assertEqual("language", groups["US| AT&T"]["for_you"])    # English, like the pool
            self.assertEqual("country", groups["CA| CINEMA FR"]["for_you"])
            self.assertIsNone(groups["ES| F1 PPV ᴿᴬᵂ"]["for_you"])
            us = {g["name"]: g for g in N.live_overview(self.db, country="US")["groups"]}
            self.assertEqual("country", us["US| AT&T"]["for_you"])
        es = groups["ES| F1 PPV ᴿᴬᵂ"]
        self.assertEqual(("F1", ["Raw"], "Spain", "ES", True),
                         (es["title"], es["tags"], es["place"]["name"], es["place"]["iso"], es["event_group"]))

    def test_samples_show_a_few_channels_and_only_events_that_are_on(self):
        groups = {g["name"]: g for g in N.live_overview(self.db)["groups"]}
        names = [s["name"] for s in groups["US| AT&T"]["samples"]]
        self.assertEqual(3, len(names))                  # TSN 1, HGTV, CNN; no separator
        self.assertIn("CNN", names)
        self.assertEqual("CNN", names[-1])               # not checked yet: last
        ppv = groups["UK| PPV"]
        self.assertTrue(ppv["event_group"])
        self.assertEqual(1, len(ppv["samples"]))         # the empty slot says nothing
        self.assertTrue(ppv["samples"][0]["event"])

    def test_step_two_shows_folded_channels_and_what_is_in_the_pool_already(self):
        out = N.channels_of(self.db, [self.group("US| AT&T").id, self.group("UK| PPV").id])
        by_name = {c["name"]: c for c in out["channels"]}
        self.assertIn("HGTV", by_name)
        hgtv = by_name["HGTV"]
        self.assertEqual(2, hgtv["feeds"])
        self.assertEqual(sorted([self.h1.id, self.h2.id]), sorted(hgtv["channel_ids"]))
        self.assertEqual("works", hgtv["check"]["state"])
        self.assertTrue(hgtv["has_guide"])
        tsn = [c for c in out["channels"] if self.c_tsn.id in c["channel_ids"]][0]
        self.assertTrue(tsn["in_pool"])
        self.assertFalse(by_name["CNN"]["in_pool"])
        slot = [c for c in out["channels"] if self.slot.id in c["channel_ids"]][0]
        self.assertEqual("no_event", slot["event"])
        self.assertIsNotNone(self.group("US| AT&T").looked_at)

    def test_adding_switches_on_only_the_picked_channels_and_reviews_the_groups(self):
        att, ppv = self.group("US| AT&T"), self.group("UK| PPV")
        out = N.add_to_pool(self.db, [att.id, ppv.id], [self.h1.id, self.h2.id, self.tsn.id])
        self.assertEqual(2, out["added"])            # TSN 1 (another group) is not touched
        self.db.expire_all()
        on = {c.id for c in self.db.query(mdb.LiveChannel).filter_by(enabled=True)}
        self.assertEqual({self.tsn.id, self.h1.id, self.h2.id}, on)
        self.assertEqual(("reviewed", True), (N.review_of(self.group("US| AT&T")), self.group("US| AT&T").enabled))
        self.assertEqual(("reviewed", False), (N.review_of(self.group("UK| PPV")), self.group("UK| PPV").enabled))
        self.assertEqual(0, N.counts(self.db)["live"])

    def test_dismissed_groups_leave_the_new_list_and_can_come_back(self):
        att = self.group("US| AT&T")
        N.dismiss(self.db, [att.id])
        self.assertEqual(["UK| PPV"], [g["name"] for g in N.live_overview(self.db)["groups"]])
        everything = N.live_overview(self.db, dismissed=True)
        self.assertEqual(1, everything["dismissed"])
        self.assertIn("US| AT&T", [g["name"] for g in everything["groups"]])
        # A later refresh doesn't make it new again.
        self.sync_groups([("1", "CA| SPORTS"), ("2", "US| AT&T"), ("3", "UK| PPV")])
        self.assertEqual("dismissed", N.review_of(self.group("US| AT&T")))
        N.restore(self.db, [att.id])
        self.assertEqual("new", N.review_of(self.group("US| AT&T")))

    def test_the_checker_tries_new_groups_channels_before_rechecking_the_pool(self):
        self.tsn.check_at = datetime.utcnow() - timedelta(days=2)
        self.tsn.check_state = "works"
        self.db.commit()
        with mock.patch.object(K, "_pool_channel_ids", lambda db: [self.tsn.id]):
            due = K.candidates(self.db)
        self.assertEqual(self.tsn.id, due[-1])
        self.assertIn(self.cnn.id, due)
        self.assertNotIn(self.h2.id, due)            # has a verdict
        self.assertLess(due.index(self.cnn.id), due.index(self.tsn.id))

    def test_unread_groups_are_read_from_the_provider_switched_off(self):
        ppv = self.group("UK| PPV")
        ppv.channels_read_at = None
        ppv.category_id = "3"
        self.db.commit()

        class FakeClient:
            def __init__(self, **kw):
                pass

            def get_live_streams(self, category_id=None):
                return [{"stream_id": 30, "name": "EPL 03 | 5:30pm Spurs vs Leeds", "category_id": category_id}]

            def live_stream_url(self, sid, extension="ts"):
                return f"http://192.0.2.10/live/u/p/{sid}.{extension}"

            def close(self):
                pass
        with mock.patch("services.xtream_client.XtreamClient", FakeClient):
            N.read_channels(self.db, [ppv])
        ch = self.db.query(mdb.LiveChannel).filter_by(stream_id="30").one()
        self.assertEqual(("UK| PPV", False), (ch.group_title, ch.enabled))
        self.assertIsNotNone(self.group("UK| PPV").channels_read_at)


class NewForPeople(Case):
    def test_channels_new_to_the_pool_are_new_for_two_weeks(self):
        a = self.channel(1, "CA EN: CNN", "G", enabled=True)
        b = self.channel(2, "US: HGTV", "G")
        entries = [{"key": f"c{a.id}"}, {"key": "n7"}]
        C._mark_new(self.db, entries)
        self.assertEqual([False, False], [e["new"] for e in entries])   # the first record is the baseline
        entries = [{"key": f"c{a.id}"}, {"key": f"c{b.id}"}, {"key": "n8"}]
        C._mark_new(self.db, entries)
        self.assertEqual([False, True, True], [e["new"] for e in entries])
        later = datetime.utcnow() + timedelta(days=C.NEW_DAYS + 1)
        C._mark_new(self.db, entries, now=later)
        self.assertEqual([False, False, False], [e["new"] for e in entries])

    def test_a_key_that_took_over_a_known_one_is_not_new(self):
        C._mark_new(self.db, [{"key": "c1"}])
        self.db.add(mdb.LiveKeyAlias(old_key="c1", new_key="n5"))
        self.db.commit()
        C._invalidate()
        entries = [{"key": "n5"}]
        C._mark_new(self.db, entries)
        self.assertFalse(entries[0]["new"])


class Vod(Case):
    def test_add_whitelists_and_dismiss_does_not(self):
        cats = []
        for i, name in enumerate(["Netflix", "Hulu"]):
            c = mdb.ProviderCategory(provider_id=self.p.id, category_id=str(i), category_name=name, type="movie",
                                     review_state="new", first_seen_at=datetime(2026, 10, 1))
            self.db.add(c)
            cats.append(c)
        mdb.set_setting(self.db, N._MIGRATED_KEY, "x")
        self.db.commit()
        N.vod_decide(self.db, [cats[0].id], add=True)
        N.vod_decide(self.db, [cats[1].id], add=False)
        self.db.expire_all()
        self.assertEqual((True, "reviewed"), (cats[0].whitelisted, N.review_of(cats[0])))
        self.assertEqual((False, "dismissed"), (cats[1].whitelisted, N.review_of(cats[1])))
        self.assertEqual(0, N.counts(self.db)["vod"])
        self.assertEqual(1, N.vod_overview(self.db, dismissed=True)["dismissed"])


if __name__ == "__main__":
    unittest.main()
