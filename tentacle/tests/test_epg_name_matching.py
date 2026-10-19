"""Guide data is matched by override, tvg-id, then channel name (#141).

Run from the tentacle/ directory:  python -m unittest discover -s tests

Matching on tvg-id alone left a channel with no tvg-id, or a tvg-id the feed
does not carry, without a guide and without a word: on a live install 459 of
840 enabled channels, and the sync still said it succeeded.
"""
import json
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
import services.xmltv as xmltv
from services.channel_names import channel_country, channel_name_key, feed_countries
from services.epg_match import coverage_report, coverage_summary, resolve_guide_ids
from tmp_dirs import temp_dir


def setUpModule():
    logging.disable(logging.CRITICAL)


def tearDownModule():
    logging.disable(logging.NOTSET)


class NameKeys(unittest.TestCase):
    def test_provider_decorations_fold_away(self):
        for provider, feed in (("CA: TSN 5 ᴿᴬᵂ", "TSN 5"), ("CA EN: DISCOVERY HD", "Discovery"),
                               ("LT: BTV HD", "BTV"), ("|US| FOX", "FOX"), ("[UK] BBC One", "BBC One"),
                               ("Šiaulių TV", "Siauliu TV"), ("TSN5 FHD", "TSN 5")):
            self.assertEqual(channel_name_key(feed), channel_name_key(provider), provider)

    def test_a_brand_with_a_colon_keeps_its_name(self):
        self.assertEqual("cnninternational", channel_name_key("CNN: International"))

    def test_nothing_distinctive_gives_no_key(self):
        self.assertEqual("", channel_name_key("HD"))
        self.assertEqual("", channel_name_key(""))


def _ch(cid, name, tvg=None, override=None, enabled=True):
    return {"id": cid, "name": name, "tvg_id": tvg, "override": override, "enabled": enabled}


FEED = [
    {"id": "TSN5.ca", "names": ["TSN 5"]},
    {"id": "AMC.ca", "names": ["AMC"]},
    {"id": "btv.lt", "names": ["BTV", "BTV HD"]},
    {"id": "fox.a", "names": ["FOX"]},
    {"id": "fox.b", "names": ["Fox"]},          # two feed channels share a key
]


class Resolve(unittest.TestCase):
    def test_passes_in_order(self):
        r = resolve_guide_ids([
            _ch(1, "anything", tvg="AMC.ca", override="TSN5.ca"),   # override wins
            _ch(2, "CA: AMC", tvg="AMC.ca"),                        # tvg-id in feed
            _ch(3, "LT: BTV HD"),                                   # no tvg-id: by name
            _ch(4, "CA: TSN 5 ᴿᴬᵂ", tvg="tsn5-missing.ca"),         # tvg-id not in feed: by name
        ], FEED)
        self.assertEqual(("override", "TSN5.ca"), (r[1]["method"], r[1]["guide_id"]))
        self.assertEqual(("tvg-id", "AMC.ca"), (r[2]["method"], r[2]["guide_id"]))
        self.assertEqual(("name", "btv.lt"), (r[3]["method"], r[3]["name_match"]))
        self.assertEqual(("name", "TSN5.ca"), (r[4]["method"], r[4]["name_match"]))

    def test_a_key_two_feed_channels_share_is_never_guessed(self):
        r = resolve_guide_ids([_ch(1, "US: FOX HD")], FEED)
        self.assertIsNone(r[1]["guide_id"])
        self.assertEqual("ambiguous", r[1]["reason"])
        self.assertEqual(["fox.a", "fox.b"], r[1]["candidates"])

    def test_a_key_two_of_our_channels_share_is_never_guessed(self):
        r = resolve_guide_ids([_ch(1, "LT: BTV"), _ch(2, "LT: BTV HD")], FEED)
        self.assertEqual({"ambiguous"}, {r[1]["reason"], r[2]["reason"]})

    def test_a_tvg_id_match_does_not_count_against_a_name_match(self):
        """TSN 5 has its tvg-id; the raw copy has none and takes TSN 5's guide by name."""
        r = resolve_guide_ids([_ch(1, "CA: TSN 5", tvg="TSN5.ca"), _ch(2, "CA: TSN 5 ᴿᴬᵂ")], FEED)
        self.assertEqual("TSN5.ca", r[1]["guide_id"])
        self.assertEqual(("name", "TSN5.ca"), (r[2]["method"], r[2]["guide_id"]))

    def test_unmatched_reasons(self):
        r = resolve_guide_ids([_ch(1, "Nothing Like It"), _ch(2, "Nope", tvg="nope.id")], FEED)
        self.assertEqual("no-tvg-id", r[1]["reason"])
        self.assertEqual("tvg-id-not-in-feed", r[2]["reason"])

    def test_a_feed_without_channel_elements_keeps_tvg_ids_on_trust(self):
        r = resolve_guide_ids([_ch(1, "AMC", tvg="AMC.ca")], [])
        self.assertEqual(("tvg-id", "AMC.ca"), (r[1]["method"], r[1]["guide_id"]))

    def test_coverage_counts_and_explains(self):
        chans = [_ch(1, "CA: AMC", tvg="AMC.ca"), _ch(2, "LT: BTV HD"), _ch(3, "Nothing"),
                 _ch(4, "Gone", tvg="nope.id"), _ch(5, "US: FOX"), _ch(6, "Off", enabled=False)]
        r = resolve_guide_ids(chans, FEED)
        report = coverage_report(chans, r, {"AMC.ca", "btv.lt"})
        e = report["enabled"]
        self.assertEqual((5, 2, 1, 1, 1, 1, 1),
                         (e["channels"], e["with_guide"], e["by_tvg_id"], e["by_name"],
                          e["no_tvg_id"], e["tvg_id_not_in_feed"], e["ambiguous"]))
        self.assertEqual({3, 4, 5}, {w["channel_id"] for w in report["without_guide"]})
        self.assertIn("guide for 2 of 5 enabled channels", coverage_summary(report))

    def test_one_tvg_id_on_unrelated_channels_is_flagged(self):
        """Nine unrelated sports channels all carried the provider tvg-id "TS"."""
        chans = [_ch(i, n, tvg="TS") for i, n in enumerate(["Sky Sports PL", "UFC", "beIN AU 1"], 1)]
        r = resolve_guide_ids(chans, [{"id": "TS", "names": ["TimeShift"]}])
        report = coverage_report(chans, r, {"TS"})
        self.assertEqual("TS", report["shared_tvg_ids"][0]["tvg_id"])
        self.assertEqual(3, report["shared_tvg_ids"][0]["channels"])


class CountryCheck(unittest.TestCase):
    """The key strips "UK:", so a channel whose only namesake in the feed was
    another country's took that guide, and DVR rules booked from it (#141)."""

    def test_a_single_foreign_namesake_is_not_taken(self):
        feed = [{"id": "SkyOne.de", "names": ["Sky One"]}, {"id": "beINSports.us", "names": ["beIN Sports"]}]
        r = resolve_guide_ids([_ch(1, "UK: SKY ONE"), _ch(2, "CA EN: BEIN SPORTS")], feed)
        self.assertEqual(("foreign", None), (r[1]["reason"], r[1]["guide_id"]))
        self.assertEqual(["SkyOne.de"], r[1]["candidates"])
        self.assertEqual("foreign", r[2]["reason"])

    def test_the_own_country_wins_where_both_exist(self):
        feed = [{"id": "CTV.ca", "names": ["CTV"]}, {"id": "CTV.us", "names": ["CTV"]}]
        r = resolve_guide_ids([_ch(1, "CA: CTV")], feed)
        self.assertEqual(("name", "CTV.ca"), (r[1]["method"], r[1]["guide_id"]))

    def test_uk_and_gb_are_one_country(self):
        r = resolve_guide_ids([_ch(1, "UK: BBC One"), _ch(2, "|GB| ITV")],
                              [{"id": "BBCOne.gb", "names": ["BBC One"]}, {"id": "ITV.uk", "names": ["ITV"]}])
        self.assertEqual(("BBCOne.gb", "ITV.uk"), (r[1]["guide_id"], r[2]["guide_id"]))

    def test_a_display_name_tag_names_the_country_too(self):
        feed = [{"id": "sky1", "names": ["UK: Sky One"]}]
        self.assertEqual("foreign", resolve_guide_ids([_ch(1, "DE: SKY ONE")], feed)[1]["reason"])
        self.assertEqual("sky1", resolve_guide_ids([_ch(1, "UK: SKY ONE")], feed)[1]["guide_id"])

    def test_no_country_on_either_side_matches_as_before(self):
        r = resolve_guide_ids([_ch(1, "Sky One"), _ch(2, "UK: Discovery")],
                              [{"id": "SkyOne.de", "names": ["Sky One"]}, {"id": "disc", "names": ["Discovery"]}])
        self.assertEqual(("SkyOne.de", "disc"), (r[1]["guide_id"], r[2]["guide_id"]))

    def test_two_channels_never_share_one_guide_by_name(self):
        feed = [{"id": "SkyOne.uk", "names": ["Sky One", "Sky 1"]}]
        r = resolve_guide_ids([_ch(1, "UK: Sky One"), _ch(2, "UK: Sky 1 HD")], feed)
        self.assertEqual({"ambiguous"}, {r[1]["reason"], r[2]["reason"]})

    def test_the_report_lists_name_matches_and_counts_foreign_ones(self):
        chans = [_ch(1, "UK: SKY ONE"), _ch(2, "LT: BTV")]
        feed = [{"id": "SkyOne.de", "names": ["Sky One"]}, {"id": "btv.lt", "names": ["BTV"]}]
        r = resolve_guide_ids(chans, feed)
        report = coverage_report(chans, r, {"SkyOne.de", "btv.lt"})
        self.assertEqual(1, report["enabled"]["foreign"])
        self.assertEqual([{"channel_id": 2, "name": "LT: BTV", "guide_id": "btv.lt"}], report["by_name"])
        self.assertIn("1 a name the feed has only for another country", coverage_summary(report))


class LanguageTags(unittest.TestCase):
    """"EN: Discovery Channel" names a language, not a country: it was read as
    country "en", so its US namesake was "foreign" and it got no guide (#527)."""

    FEED = [{"id": "DiscoveryChannel.us", "names": ["Discovery Channel"]}]

    def test_a_language_tag_matches_like_an_untagged_name(self):
        for name in ("Discovery Channel", "EN: Discovery Channel", "|EN| Discovery Channel",
                     "[EN] DISCOVERY CHANNEL HD", "JA: Discovery Channel"):
            with self.subTest(name=name):
                r = resolve_guide_ids([_ch(1, name)], self.FEED)[1]
                self.assertEqual(("name", "DiscoveryChannel.us"), (r["method"], r["guide_id"]), r["reason"])

    def test_a_country_after_the_language_still_scopes_the_match(self):
        r = resolve_guide_ids([_ch(1, "EN CA: Discovery Channel")], self.FEED)[1]
        self.assertEqual("foreign", r["reason"])
        feed = self.FEED + [{"id": "DiscoveryChannel.ca", "names": ["Discovery Channel"]}]
        r = resolve_guide_ids([_ch(1, "EN CA: Discovery Channel")], feed)[1]
        self.assertEqual(("name", "DiscoveryChannel.ca"), (r["method"], r["guide_id"]))

    def test_real_country_tags_are_unchanged(self):
        r = resolve_guide_ids([_ch(1, "UK: Discovery Channel"), _ch(2, "CA EN: Discovery Channel"),
                               _ch(3, "CA FR: Discovery Channel"), _ch(4, "LT: Discovery Channel")], self.FEED)
        self.assertEqual(["foreign"] * 4, [r[i]["reason"] for i in (1, 2, 3, 4)])
        for name, country in (("UK: X", "gb"), ("|GB| X", "gb"), ("US: X", "us"), ("USA: X", "us"),
                              ("CA: X", "ca"), ("CA EN: X", "ca"), ("CA FR: X", "ca"), ("LT: X", "lt"),
                              ("FR: X", "fr"), ("DE: X", "de"), ("AR: X", "ar"), ("EU: X", "eu"),
                              ("EN: X", None), ("[EN] X", None), ("EN CA: X", "ca"), ("X", None)):
            with self.subTest(name=name):
                self.assertEqual(country, channel_country(name))

    def test_a_language_tag_on_the_feed_side_still_names_a_country(self):
        feed = [{"id": "disc", "names": ["JA: Discovery Channel"]}]
        self.assertEqual({"ja"}, feed_countries("disc", ["JA: Discovery Channel"]))
        self.assertEqual("foreign", resolve_guide_ids([_ch(1, "US: Discovery Channel")], feed)[1]["reason"])


def _stamp(dt):
    return dt.strftime("%Y%m%d%H%M%S +0000")


class EpgSyncMatchesByName(unittest.TestCase):
    """The real sync and the real guide, with the feed in the disk cache."""

    def setUp(self):
        self.tmp = temp_dir(self)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        engine = create_engine(f"sqlite:///{self.tmp}/t.db")
        mdb.Base.metadata.create_all(engine)
        self.Session = sessionmaker(bind=engine)
        self.db = self.Session()
        self.addCleanup(self.db.close)
        p = mdb.Provider(name="P", server_url="http://192.0.2.10", username="u", password="p",
                         live_tv_enabled=True)
        self.db.add(p)
        self.db.commit()
        self.pid = p.id
        self.channels = {}
        for sid, name, tvg in (("1", "CA: AMC HD", "AMC.ca"), ("2", "LT: BTV HD", None),
                               ("3", "CA: TSN 5 ᴿᴬᵂ", "old-tsn.ca"), ("4", "US: FOX", None),
                               ("5", "Mystery", None)):
            ch = mdb.LiveChannel(provider_id=p.id, name=name, stream_id=sid, epg_channel_id=tvg,
                                 stream_url=f"http://192.0.2.10/live/{sid}.ts", enabled=True)
            self.db.add(ch)
            self.channels[sid] = ch
        self.db.commit()
        patcher = mock.patch.object(xmltv, "XMLTV_CACHE_DIR", os.path.join(self.tmp, "cache"))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.url = "http://192.0.2.10/xmltv.php"
        start = datetime.utcnow() + timedelta(hours=1)
        progs = "".join(
            f'<programme start="{_stamp(start)}" stop="{_stamp(start + timedelta(hours=1))}" channel="{cid}">'
            f'<title>{title}</title></programme>'
            for cid, title in (("AMC.ca", "A film"), ("btv.lt", "Žinios"), ("TSN5.ca", "Hockey"),
                               ("fox.a", "F1"), ("fox.b", "F2")))
        feed = ('<tv><channel id="AMC.ca"><display-name>AMC</display-name></channel>'
                '<channel id="btv.lt"><display-name>BTV</display-name></channel>'
                '<channel id="TSN5.ca"><display-name>TSN 5</display-name></channel>'
                '<channel id="fox.a"><display-name>FOX</display-name></channel>'
                '<channel id="fox.b"><display-name>Fox</display-name></channel>' + progs + '</tv>')
        with open(xmltv._get_cache_path(self.url), "w", encoding="utf-8") as f:
            f.write(feed)

    def _sync(self):
        data = {"id": self.pid, "channels": [], "enabled_count": 5, "epg_url": self.url,
                "provider_type": "xtream", "user_agent": "t"}
        with mock.patch.object(livetv_router, "SessionLocal", self.Session):
            return livetv_router._run_epg_sync_background(data)

    def _client(self):
        app = FastAPI()
        app.include_router(livetv_router.router)
        app.dependency_overrides[mdb.get_db] = lambda: self.db
        app.dependency_overrides[livetv_router.require_admin] = lambda: None
        return TestClient(app)

    def test_channels_without_a_usable_tvg_id_get_a_guide_by_name(self):
        self.assertTrue(self._sync())
        self.db.expire_all()
        by_sid = {c.stream_id: c for c in self.db.query(mdb.LiveChannel)}
        self.assertEqual("AMC.ca", by_sid["1"].guide_epg_id)
        self.assertEqual(("btv.lt", "name"), (by_sid["2"].guide_epg_id, by_sid["2"].epg_match))
        self.assertEqual(("TSN5.ca", "name"), (by_sid["3"].guide_epg_id, by_sid["3"].epg_match))
        self.assertIsNone(by_sid["4"].guide_epg_id, "FOX is ambiguous in the feed: no guess")
        self.assertIsNone(by_sid["5"].guide_epg_id)

        xml = self._client().get("/hdhr/xmltv.xml").text
        for title in ("A film", "Žinios", "Hockey"):
            self.assertIn(f"<title>{title}</title>", xml)
        self.assertNotIn("<title>F1</title>", xml)

    def _add_own_tsn_schedule(self):
        """The feed carries TSN 5's own schedule (its tvg-id) without a
        <channel> element for it."""
        path = xmltv._get_cache_path(self.url)
        feed = open(path, encoding="utf-8").read()
        start = datetime.utcnow() + timedelta(hours=1)
        feed = feed.replace("</tv>", f'<programme start="{_stamp(start)}" stop="{_stamp(start + timedelta(hours=1))}" '
                                     f'channel="old-tsn.ca"><title>Own schedule</title></programme></tv>')
        with open(path, "w", encoding="utf-8") as f:
            f.write(feed)

    def test_a_tvg_id_that_brought_programmes_keeps_its_guide(self):
        """A name match must not replace the guide its own tvg-id brought."""
        self._add_own_tsn_schedule()
        self.assertTrue(self._sync())
        self.db.expire_all()
        tsn = self.db.query(mdb.LiveChannel).filter(mdb.LiveChannel.stream_id == "3").one()
        self.assertEqual("old-tsn.ca", tsn.guide_epg_id)
        self.assertIsNone(tsn.epg_name_match)
        self.assertIn("<title>Own schedule</title>", self._client().get("/hdhr/xmltv.xml").text)

    def test_the_next_sync_after_a_dropped_name_match_succeeds(self):
        """#467: the dropped match's programmes were stored under an id no
        channel used, so the next sync never deleted them and failed on
        UNIQUE(channel_id, start) for as long as the feed stayed the same."""
        self._add_own_tsn_schedule()
        self.assertTrue(self._sync())
        self.assertTrue(self._sync(), livetv_router._get_sync_status(self.pid).get("message"))
        self.db.expire_all()
        self.assertEqual(0, self.db.query(mdb.EPGProgram).filter_by(channel_id="TSN5.ca").count(),
                         "no channel's guide: not kept")
        self.assertEqual(1, self.db.query(mdb.EPGProgram).filter_by(channel_id="old-tsn.ca").count())

    def test_rows_an_earlier_sync_left_under_a_dropped_match_are_replaced(self):
        self._add_own_tsn_schedule()
        start = datetime.utcnow().replace(microsecond=0) + timedelta(hours=1)
        self.db.add(mdb.EPGProgram(channel_id="TSN5.ca", title="Hockey", start=start,
                                   stop=start + timedelta(hours=1)))
        self.db.commit()
        self.assertTrue(self._sync(), livetv_router._get_sync_status(self.pid).get("message"))
        self.db.expire_all()
        self.assertEqual(0, self.db.query(mdb.EPGProgram).filter_by(channel_id="TSN5.ca").count())

    def test_a_dropped_match_leaves_another_providers_guide_alone(self):
        """#516: programmes are keyed by guide id across providers. Q's channel
        uses TSN5.ca by tvg-id; P's dropped name match on it must not delete
        Q's guide, which P's sync doesn't store again."""
        self._add_own_tsn_schedule()
        q = mdb.Provider(name="Q", server_url="http://192.0.2.20", username="u", password="p",
                         live_tv_enabled=True)
        self.db.add(q)
        self.db.flush()
        self.db.add(mdb.LiveChannel(provider_id=q.id, name="TSN 5", stream_id="9", epg_channel_id="TSN5.ca",
                                    stream_url="http://192.0.2.20/live/9.ts", enabled=True))
        start = datetime.utcnow().replace(microsecond=0) + timedelta(hours=1)
        self.db.add(mdb.EPGProgram(channel_id="TSN5.ca", title="Q's hockey", start=start,
                                   stop=start + timedelta(hours=1)))
        self.db.commit()
        self.assertTrue(self._sync(), livetv_router._get_sync_status(self.pid).get("message"))
        self.db.expire_all()
        self.assertEqual(["Q's hockey"], [p.title for p in
                                          self.db.query(mdb.EPGProgram).filter_by(channel_id="TSN5.ca")])
        self.assertEqual(1, self.db.query(mdb.EPGProgram).filter_by(channel_id="old-tsn.ca").count())

    def test_the_channel_list_shows_how_each_guide_was_found(self):
        self._sync()
        rows = {c["stream_id"]: c for c in self._client().get("/api/live/channels").json()["channels"]}
        self.assertEqual((True, "name"), (rows["2"]["has_epg_data"], rows["2"]["epg_match"]))
        self.assertFalse(rows["5"]["has_epg_data"])
        with_epg = self._client().get("/api/live/channels", params={"has_epg": True}).json()["channels"]
        self.assertEqual({"1", "2", "3"}, {c["stream_id"] for c in with_epg})

    def test_a_coverage_report_follows_the_sync(self):
        self._sync()
        report = self._client().get("/api/live/epg-coverage").json()["providers"][0]["report"]
        self.assertEqual(3, report["enabled"]["with_guide"])
        self.assertEqual(2, report["enabled"]["by_name"])
        self.assertEqual({"ambiguous", "no-tvg-id"}, {w["reason"] for w in report["without_guide"]})
        status = livetv_router._get_sync_status(self.pid)
        self.assertIn("guide for 3 of 5 enabled channels", status["message"])

    def test_an_override_survives_a_channel_sync_and_wins(self):
        r = self._client().put(f"/api/live/channels/{self.channels['4'].id}",
                               json={"epg_id_override": "fox.b"})
        self.assertEqual(200, r.status_code)

        class _Client:
            def live_stream_url(self, sid, extension="m3u8"):
                return f"http://192.0.2.10/live/{sid}.ts"
        livetv_router._upsert_channels(self.pid, [{"stream_id": 4, "name": "US: FOX", "category_id": ""}],
                                       {}, _Client(), self.db)
        self.db.commit()
        self._sync()
        self.db.expire_all()
        fox = self.db.query(mdb.LiveChannel).filter_by(stream_id="4").one()
        self.assertEqual(("fox.b", "override"), (fox.guide_epg_id, fox.epg_match))
        self.assertIn("<title>F2</title>", self._client().get("/hdhr/xmltv.xml").text)

    def test_a_name_match_that_moves_leaves_no_old_programmes(self):
        self._sync()
        self.db.query(mdb.LiveChannel).filter_by(stream_id="2").update({"epg_id_override": "AMC.ca"})
        self.db.commit()
        self._sync()
        self.db.expire_all()
        self.assertEqual(0, self.db.query(mdb.EPGProgram).filter_by(channel_id="btv.lt").count())


if __name__ == "__main__":
    unittest.main()
