"""Moving an install off the old HDHomeRun tuner onto the M3U lineup must
not lose what people booked, favourited, or the guide another source gives.

livetv_tuners.sync() removes the old HDHomeRun tuner in the same run that
adds the lineup tuner (`to_remove = list(olds)`). Recording schedules on the
old channels are moved only when the new channel's guide already has the
programme (move_schedules); otherwise they are logged "kept" -- and the tuner
they are on is deleted anyway, so Jellyfin drops the one-off timers at its
next guide refresh (DefaultLiveTvService.RefreshTimers) and series timers are
left on a channel that no longer exists. Per-person M3U tuners already have
the right rule (they stay while a schedule is on them).

The fake Jellyfin is the one in test_livetv_lineup.py, with two things the
real 10.11.8 does that it didn't model yet: a listing provider posted with an
Id is saved in place (ListingsManager.SaveListingProvider), and
POST /LiveTv/ChannelMappings sets one mapping (SetChannelMapping).

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import os
from unittest import mock

import models.database as mdb
from services import livetv_lineups as L
from services import livetv_tuners as T

import test_livetv_lineup as lineup_tests
from test_livetv_lineup import ADMIN, ANN, BASE, BEN, LINEUP

OTHER_GUIDE = "http://guide.example/epg.xml"   # a guide source that isn't Tentacle's


class Jellyfin1011(lineup_tests.FakeJellyfin):
    def post(self, path, body=None, **p):
        if path == "/LiveTv/ListingProviders" and body.get("Id"):
            self.calls.append(("POST", path, body))
            self.listings = [dict(body) if lp.get("Id") == body["Id"] else lp for lp in self.listings]
            return body
        if path == "/LiveTv/ChannelMappings":
            self.calls.append(("POST", path, body))
            lp = next(lp for lp in self.listings if lp.get("Id") == body["ProviderId"])
            lp["ChannelMappings"] = [m for m in lp.get("ChannelMappings") or []
                                     if m["Name"] != body["TunerChannelId"]] + [
                {"Name": body["TunerChannelId"], "Value": body["ProviderChannelId"]}]
            return {}
        return super().post(path, body, **p)


class HdhrUpgrade(lineup_tests.DbCase):
    def setUp(self):
        super().setUp()
        self.jf = Jellyfin1011(self.Session, [(ADMIN, "admin"), (ANN, "ann"), (BEN, "ben")])
        for name, value in (("jellyfin_for", lambda db: self.jf),
                            ("refresh_and_wait", lambda db, jf: jf.refresh() or True),
                            ("wait_for_guide", lambda jf: True),
                            ("BACKUP_DIR", os.path.join(self.tmp, "backups"))):
            pt = mock.patch.object(T, name, value)
            pt.start()
            self.addCleanup(pt.stop)
        pt = mock.patch("services.jellyfin_guide.refresh_jellyfin_guide", lambda *a, **k: self.jf.refresh())
        pt.start()
        self.addCleanup(pt.stop)

    def sync(self):
        return T.sync(self.db)

    def ids(self):
        """The lineup's Jellyfin ids by key."""
        return {k: i for i, k in L.item_ids_for(LINEUP, L.lineup(self.db)).items()}

    def v190_hdhr(self):
        """Jellyfin as v1.9.0 left it: one HDHomeRun tuner carrying EVERY
        enabled channel (GuideNumber = stream_id), not the folded pool.
        Returns stream_id -> Jellyfin item id."""
        self.jf.tuners.append({"Id": "hd", "Type": "hdhomerun", "Url": BASE, "DeviceId": "TENTACLE1"})
        ids = {}
        for ch in self.db.query(mdb.LiveChannel).filter(mdb.LiveChannel.enabled == True).all():  # noqa: E712
            i = L.channel_item_id(L.hdhr_external_id(ch.stream_id))
            self.jf.channels[i] = {"Id": i, "Name": ch.name, "Tags": [], "tuner": BASE}
            ids[ch.stream_id] = i
        return ids

    def no_guide_yet(self):
        """The new channels have no programmes yet (Tentacle's guide doesn't
        cover them, or the nightly guide download failed)."""
        get = self.jf.get
        self.jf.get = lambda path, **p: {"Items": []} if path == "/LiveTv/Programs" else get(path, **p)

    # ── F-A1: recordings ────────────────────────────────────────────────────

    def test_a_one_off_recording_that_cant_move_yet_is_not_lost(self):
        hd = self.v190_hdhr()
        self.no_guide_yet()
        self.jf.timers.append({"Id": "once", "Name": "Basketball", "ChannelId": hd["102"],
                               "StartDate": "2026-10-24T13:30:00.0000000Z",
                               "EndDate": "2026-10-24T15:30:00.0000000Z"})
        self.sync()
        # Still booked: moved onto the lineup's channel, or left on the old
        # tuner, which then stays until it can move.
        self.assertEqual(1, len(self.jf.timers), "the booked recording was deleted")
        self.assertIn(self.jf.timers[0]["ChannelId"], self.jf.channels)

    def test_a_series_schedule_that_cant_move_yet_keeps_a_channel(self):
        hd = self.v190_hdhr()
        self.no_guide_yet()
        self.jf.series.append({"Id": "old", "Name": "Hockey Night", "ChannelId": hd["102"], "KeepUpTo": 3})
        self.sync()
        self.assertEqual(1, len(self.jf.series))
        self.assertIn(self.jf.series[0]["ChannelId"], self.jf.channels,
                      "the series schedule points at a channel Jellyfin no longer has")

    # ── F-A2: another guide source on the old tuner ─────────────────────────

    def test_another_guide_source_follows_the_channels_to_the_lineup(self):
        self.v190_hdhr()
        self.jf.listings.append({"Type": "xmltv", "Path": OTHER_GUIDE, "Id": "other",
                                 "EnableAllTuners": False, "EnabledTuners": ["hd"],
                                 "ChannelMappings": [{"Name": "hdhr_102", "Value": "sport.example"}]})
        self.sync()
        other = next(lp for lp in self.jf.listings if lp.get("Path") == OTHER_GUIDE)
        lineup = next(t for t in self.jf.tuners if t["Url"] == LINEUP)
        self.assertTrue(other.get("EnableAllTuners") or lineup["Id"] in other.get("EnabledTuners", []),
                        f"guide source still enabled only for {other.get('EnabledTuners')}; "
                        f"tuners now {[t['Id'] for t in self.jf.tuners]}")
        # Jellyfin matches a mapping on the tuner channel's id, then its tvg-id
        # (ListingsManager.GetEpgChannelFromTunerChannel): either keeps it.
        mapped = {m["Name"]: m["Value"] for m in other.get("ChannelMappings") or []}
        sport = L.m3u_external_id(LINEUP, BASE + "/api/live/stream/2")
        self.assertEqual("sport.example", mapped.get(sport) or mapped.get("102"),
                         f"no mapping for the lineup's channel: {mapped}")

    # ── F-A3: favourites on a folded channel ────────────────────────────────

    def test_a_favourite_on_any_member_of_a_folded_network_is_carried(self):
        p = self.db.query(mdb.Provider).one()
        for sid, name in (("201", "CA: TSN 4 HD"), ("202", "CA: TSN 4 ᴿᴬᵂ")):
            self.db.add(mdb.LiveChannel(provider_id=p.id, name=name, stream_id=sid, enabled=True,
                                        stream_url=f"http://192.0.2.10/live/{sid}.ts", group_title="CA| SPORTS"))
        self.db.commit()
        net = next(c for c in L.pool(self.db) if c["key"].startswith("n"))
        self.assertIn(net["xmltv_id"], ("201", "202"))
        member = ({"201", "202"} - {net["xmltv_id"]}).pop()      # the one the network isn't numbered by
        hd = self.v190_hdhr()
        self.jf.favs[ANN] = {hd[member]}
        self.sync()
        self.assertEqual({self.ids()[net["key"]]}, self.jf.favourites(ANN))
