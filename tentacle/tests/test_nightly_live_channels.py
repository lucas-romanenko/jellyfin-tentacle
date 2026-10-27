"""The nightly job reads the Live TV channels again (Xtream), not only the groups.

Run from the tentacle/ directory:  python -m unittest discover -s tests

A channel's name changed only when someone pressed the Live TV page's
channel sync. An event channel is named for the day's game ("ESPN+ 07: Leafs
at Bruins Oct 26 7:00PM ET"), so the names went stale: on 26 Oct 2026 the
channels still carried 23 Oct's games, the lineup left them out as past
events, and that evening's Leafs game wasn't in Jellyfin. The nightly job
now runs the channel sync of every Xtream Live TV provider after its group
refresh and before the guide download, then has Jellyfin refresh its guide
as the Live TV page does after a channel sync.
"""
import logging
import unittest
from pathlib import Path
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb
import routers.livetv as livetv
import services.discovery as discovery
import services.xtream_client as xc
from models.database import ActivityLog, LiveChannel, LiveChannelGroup, Provider
from tmp_dirs import temp_dir

try:
    import main
    import routers.sync  # noqa: F401  (imported inside run_scheduled_sync)
except Exception:  # pragma: no cover - depends on optional deps
    main = None


STREAMS = {
    "10": [{"stream_id": 501, "name": "ESPN+ 07: Leafs at Bruins Oct 26 7:00PM ET", "category_id": "10"},
           {"stream_id": 502, "name": "ESPN+ 08: Oilers at Flames Oct 26 9:00PM ET", "category_id": "10"}],
    "20": [{"stream_id": 601, "name": "NEWS ONE", "category_id": "20"}],
}


class _Client:
    """The provider: listings per category. `fail` makes every listing raise."""
    made = []
    asked = []
    fail = False

    def __init__(self, **kwargs):
        self.guard = xc.RequestGuard()
        _Client.made.append(self)

    def get_live_streams(self, category_id=None):
        _Client.asked.append(str(category_id))
        if _Client.fail:
            raise RuntimeError("panel down")
        return [dict(s) for s in STREAMS.get(str(category_id), [])]

    def live_stream_url(self, sid, extension="m3u8"):
        return f"http://192.0.2.10/live/u/p/{sid}.{extension}"

    def close(self):
        pass


class _Base(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        self.tmp = Path(temp_dir(self))
        engine = create_engine(f"sqlite:///{self.tmp}/t.db")
        mdb.Base.metadata.create_all(engine)
        self.addCleanup(engine.dispose)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)
        # Set up on the Live TV page: VOD off, Live TV on.
        p = Provider(name="Bears", provider_type="xtream", server_url="http://192.0.2.10",
                     username="u", password="p", active=False, live_tv_enabled=True)
        self.db.add(p)
        self.db.commit()
        self.pid = p.id
        self.db.add_all([
            LiveChannelGroup(provider_id=p.id, name="ESPN+", category_id="10", enabled=True),
            LiveChannelGroup(provider_id=p.id, name="NEWS", category_id="20", enabled=False),
            # Three days old, switched on and numbered, one with the admin's own name
            LiveChannel(provider_id=p.id, name="ESPN+ 07: Princeton vs. RPI Oct 23 6:00PM ET", stream_id="501",
                        stream_url="http://192.0.2.10/live/u/p/501.m3u8", group_title="ESPN+",
                        enabled=True, channel_number=7),
            LiveChannel(provider_id=p.id, name="ESPN+ 08: Oct 23", stream_id="502", custom_name="Game 8",
                        stream_url="http://192.0.2.10/live/u/p/502.m3u8", group_title="ESPN+",
                        enabled=False, channel_number=8),
        ])
        self.db.commit()
        orig = xc.XtreamClient
        xc.XtreamClient = _Client
        self.addCleanup(setattr, xc, "XtreamClient", orig)
        _Client.made, _Client.asked, _Client.fail = [], [], False
        self.addCleanup(livetv._sync_status.clear)
        p = mock.patch("services.provider_activity.recording_protected", lambda db: False)
        p.start()
        self.addCleanup(p.stop)

    def channel(self, sid):
        self.db.expire_all()
        return self.db.query(LiveChannel).filter_by(provider_id=self.pid, stream_id=sid).one()


class RefreshLiveChannels(_Base):
    def test_names_follow_the_provider_and_the_admins_choices_stay(self):
        self.assertEqual([self.pid], discovery.refresh_live_channels(self.db, gentle=True))
        ch = self.channel("501")
        self.assertEqual("ESPN+ 07: Leafs at Bruins Oct 26 7:00PM ET", ch.name)
        self.assertTrue(ch.enabled)
        self.assertEqual(7, ch.channel_number)
        ch = self.channel("502")
        self.assertEqual("ESPN+ 08: Oilers at Flames Oct 26 9:00PM ET", ch.name)
        self.assertEqual("Game 8", ch.custom_name)
        self.assertFalse(ch.enabled, "a channel switched off stays off")
        self.assertEqual(8, ch.channel_number)

    def test_only_enabled_groups_are_read_gently(self):
        discovery.refresh_live_channels(self.db, gentle=True)
        self.assertEqual(["10"], _Client.asked)
        self.assertGreaterEqual(_Client.made[0].guard.gap, discovery.CHANNEL_GAP_SECONDS)

    def test_its_status_ends_and_activity_says_so(self):
        discovery.refresh_live_channels(self.db)
        self.assertEqual("complete", livetv._get_sync_status(self.pid)["phase"])
        msgs = [a.message for a in self.db.query(ActivityLog)]
        self.assertIn("Live TV channel sync for Bears: 0 new, 2 updated, 2 total", msgs)

    def test_a_new_channel_in_an_enabled_group_comes_in_switched_on(self):
        STREAMS["10"].append({"stream_id": 503, "name": "ESPN+ 09: Kraken at Jets", "category_id": "10"})
        self.addCleanup(STREAMS["10"].pop)
        discovery.refresh_live_channels(self.db)
        self.assertTrue(self.channel("503").enabled)

    def test_a_failure_is_said_and_never_left_running(self):
        _Client.fail = True
        self.assertEqual([], discovery.refresh_live_channels(self.db))
        self.assertEqual("error", livetv._get_sync_status(self.pid)["phase"])
        self.assertEqual("ESPN+ 07: Princeton vs. RPI Oct 23 6:00PM ET", self.channel("501").name)
        self.assertTrue(any("Nightly Live TV channel sync for Bears failed" in a.message
                            for a in self.db.query(ActivityLog)))

    def test_a_sync_already_running_is_left_alone(self):
        livetv._set_sync_status(self.pid, {"phase": "running", "progress": 40, "message": "Fetching"})
        self.assertEqual([], discovery.refresh_live_channels(self.db))
        self.assertEqual([], _Client.asked)
        self.assertEqual("running", livetv._get_sync_status(self.pid)["phase"])

    def test_it_stands_aside_for_a_protected_recording(self):
        with mock.patch("services.provider_activity.recording_protected", lambda db: True):
            self.assertEqual([], discovery.refresh_live_channels(self.db))
        self.assertEqual([], _Client.asked)

    def test_no_enabled_group_no_request(self):
        self.db.query(LiveChannelGroup).update({"enabled": False})
        self.db.commit()
        self.assertEqual([], discovery.refresh_live_channels(self.db))
        self.assertEqual([], _Client.asked)

    def test_m3u_providers_are_left_to_the_playlist_read(self):
        self.db.query(Provider).update({"provider_type": "m3u_url"})
        self.db.commit()
        self.assertEqual([], discovery.refresh_live_channels(self.db))
        self.assertEqual([], _Client.asked)


class GuideRefreshAfterSync(_Base):
    def test_it_is_the_live_tv_pages_refresh(self):
        mdb.set_setting(self.db, "jellyfin_url", "http://192.0.2.20:8096")
        mdb.set_setting(self.db, "jellyfin_api_key", "k")
        with mock.patch("services.jellyfin_guide.refresh_jellyfin_guide") as refresh:
            self.assertTrue(livetv.refresh_jellyfin_guide_after_sync(self.db))
        refresh.assert_called_once_with("http://192.0.2.20:8096", "k", then_tag=True)

    def test_no_jellyfin_nothing_asked(self):
        with mock.patch("services.jellyfin_guide.refresh_jellyfin_guide") as refresh:
            self.assertFalse(livetv.refresh_jellyfin_guide_after_sync(self.db))
        refresh.assert_not_called()

    def test_a_failure_never_raises(self):
        mdb.set_setting(self.db, "jellyfin_url", "http://192.0.2.20:8096")
        mdb.set_setting(self.db, "jellyfin_api_key", "k")
        with mock.patch("services.jellyfin_guide.refresh_jellyfin_guide", side_effect=RuntimeError("down")):
            self.assertFalse(livetv.refresh_jellyfin_guide_after_sync(self.db))


@unittest.skipIf(main is None, "fastapi/apscheduler not installed")
class TheNightlyJob(_Base):
    def run_night(self, epg_ok=True):
        import services.jellyfin as jellyfin
        import services.smartlists as sl
        import services.radarr as radarr
        import services.sonarr as sonarr
        import services.tagger as tagger

        steps = []
        real_refresh = discovery.refresh_live_channels

        def channels(db, gentle=False):
            steps.append(("channels", gentle))
            return real_refresh(db, gentle=gentle)

        def epg(data):
            steps.append(("epg", self.channel("501").name))
            return epg_ok

        patches = [
            mock.patch.object(main, "SessionLocal", lambda: self.db),
            mock.patch.object(discovery, "discover_new_provider_content",
                              lambda db, gentle=False: steps.append(("groups", gentle))
                              or {"vod_new": [], "live_new": []}),
            mock.patch.object(discovery, "refresh_live_channels", channels),
            mock.patch.object(livetv, "_run_epg_sync_background", epg),
            mock.patch.object(livetv, "refresh_jellyfin_guide_after_sync",
                              lambda db: steps.append(("jellyfin guide", None)) or True),
            mock.patch.object(sl, "refresh_smartlist_playlists", lambda db, user_id=None, only_names=None: {}),
            mock.patch.object(sl, "sync_smartlists", lambda db, user_id=None: {}),
            mock.patch.object(sl, "write_home_config", lambda db, user_id=None: {}),
            mock.patch.object(sl, "migrate_global_smartlists_to_user", lambda db, uid: None),
            mock.patch.object(sl, "cleanup_orphaned_playlists", lambda db, uid: 0),
            mock.patch.object(sl, "_notify_jellyfin_plugin", lambda db, user=None: {}),
            mock.patch.object(jellyfin, "push_tags_to_jellyfin", lambda db, log_prefix="": 0),
            mock.patch.object(jellyfin, "sweep_orphaned_downloads", lambda db: 0),
            mock.patch.object(radarr, "scan_radarr_library", lambda db: {}),
            mock.patch.object(sonarr, "scan_sonarr_library", lambda db: {}),
            mock.patch.object(tagger, "refresh_recently_added_tags", lambda db: None),
            mock.patch.object(main, "_quiet_before_guide", lambda since: 0.0),
            # Provider jobs wait while live TV runs: a stream another test left
            # open would make this one wait for hours.
            mock.patch("services.provider_activity.live_streams_active", lambda: False),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        main.run_scheduled_sync()
        return steps

    def test_groups_then_channels_then_the_guide_then_jellyfin(self):
        steps = self.run_night()
        self.assertEqual([("groups", True), ("channels", True),
                          ("epg", "ESPN+ 07: Leafs at Bruins Oct 26 7:00PM ET"),
                          ("jellyfin guide", None)], steps)
        self.assertTrue(any(a.message.startswith("Live TV channel sync for Bears")
                            for a in self.db.query(ActivityLog)))

    def test_new_names_reach_jellyfin_even_when_the_guide_download_fails(self):
        steps = self.run_night(epg_ok=False)
        self.assertEqual(("jellyfin guide", None), steps[-1])


if __name__ == "__main__":
    unittest.main()
