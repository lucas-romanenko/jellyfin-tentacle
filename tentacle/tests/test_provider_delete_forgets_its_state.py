"""Deleting a provider must not leave state keyed by its id for the next
provider to inherit (#625).

Run from the tentacle/ directory:  python -m unittest discover -s tests

SQLite hands the next provider the same id when the deleted one had the
highest id (providers has no AUTOINCREMENT keyword), and deleting a provider
and adding it again is the usual answer to "my provider gave me a new
address". delete_provider() removes the provider's own rows but left:
- settings livetv_m3u_switched_off_<id> (#158), livetv_epg_coverage_<id>,
  livetv_epg_synced_<id>, livetv_output_formats_<id>: a channel switched off
  on the OLD provider arrives switched off in an enabled group on the new one,
  and the Live TV page shows the old coverage until the first guide sync;
- the provider's entry in provider_relink_pending (#149);
- its entry in provider_health: the new provider starts "failing since ..."
  and its first good check sends "works again";
- its blocked streams and match fixes: the new provider's stream with the same
  id is skipped, or imported as the old provider's film.
"""
import json
import logging
import shutil
import unittest
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb
import routers.livetv as livetv
from models.database import (BlockedStream, LiveChannel, LiveChannelGroup, MatchOverride,
                             get_setting, set_setting)
from routers import providers
from services import provider_health
from services.m3u_parser import parse_m3u
from tmp_dirs import temp_dir

KEYS = ("livetv_m3u_switched_off_{}", "livetv_epg_coverage_{}", "livetv_epg_synced_{}",
        "livetv_output_formats_{}")


def setUpModule():
    logging.disable(logging.CRITICAL)


def tearDownModule():
    logging.disable(logging.NOTSET)


class ProviderDeleteForgetsItsState(unittest.TestCase):
    def setUp(self):
        tmp = temp_dir(self)
        self.addCleanup(shutil.rmtree, tmp, True)
        engine = create_engine(f"sqlite:///{tmp}/t.db")
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)

    def _provider(self, name):
        p = mdb.Provider(name=name, server_url="http://192.0.2.10", username="u", password="p",
                         provider_type="m3u_url", active=False, live_tv_enabled=True)
        self.db.add(p)
        self.db.commit()
        self.db.add(LiveChannelGroup(provider_id=p.id, name="Sports", enabled=True))
        self.db.commit()
        return p.id

    def _sync(self, pid, names):
        text = "#EXTM3U\n" + "".join(
            f'#EXTINF:-1 group-title="Sports",{n}\nhttp://x/live/{n.replace(" ", "")}.ts\n' for n in names)
        livetv._upsert_channels_from_m3u(pid, parse_m3u(text), self.db)
        self.db.commit()

    def _channels(self, pid):
        self.db.expire_all()
        return {c.name: c for c in self.db.query(LiveChannel).filter_by(provider_id=pid)}

    def _delete(self, pid):
        with mock.patch("services.smartlists.sync_smartlists"), \
                mock.patch("services.smartlists.refresh_smartlist_playlists"), \
                mock.patch("services.smartlists.write_home_config"):
            providers.delete_provider(pid, db=self.db)

    def _same_id(self, old, new):
        if new != old:
            self.skipTest("the database did not reuse the id")

    def test_live_tv_settings_and_pending_rewrite_go_with_the_provider(self):
        lineup = [f"Channel {i}" for i in range(30)]
        old = self._provider("Old address")
        self._sync(old, lineup)
        ch = self._channels(old)["Channel 3"]
        ch.enabled = False                                   # the user's own "off"
        self.db.commit()
        self._sync(old, [n for n in lineup if n != "Channel 3"])  # the old provider drops it
        self.assertEqual(["Channel 3"], livetv._m3u_switched_off(self.db, old))
        set_setting(self.db, f"livetv_epg_coverage_{old}", '{"enabled": 30, "with_guide": 30}')
        set_setting(self.db, f"livetv_epg_synced_{old}", '"2026-10-01T03:00:00"')
        set_setting(self.db, f"livetv_output_formats_{old}", '["ts"]')
        # an address change saved but not rewritten yet (#149), plus another provider's
        set_setting(self.db, "provider_relink_pending", json.dumps({str(old): {"m3u": True},
                                                                    "99": {"m3u": True}}))
        self.db.commit()

        self._delete(old)

        new = self._provider("New address")
        self._same_id(old, new)
        leftover = [key.format(new) for key in KEYS if get_setting(self.db, key.format(new), "")]
        self._sync(new, lineup)
        self.assertTrue(self._channels(new)["Channel 3"].enabled,
                        "a channel switched off on the deleted provider arrived switched off on the new one")
        self.assertEqual([], leftover, "settings outlived the provider they belonged to")
        self.assertEqual({"99": {"m3u": True}},
                         json.loads(get_setting(self.db, "provider_relink_pending", "") or "{}"),
                         "the deleted provider's pending rewrite was kept (or another one's dropped)")

    def test_health_state_blocks_and_fixes_go_with_the_provider(self):
        old = self._provider("Old")
        other = self._provider("Other")
        failing = {**provider_health._unknown(), "name": "Old", "state": "failing",
                   "since": "2026-10-01T03:00:00+00:00", "alerted": True}
        set_setting(self.db, provider_health.SETTING_KEY,
                    json.dumps({str(old): failing, str(other): provider_health._unknown()}))
        for pid in (old, other):
            self.db.add(BlockedStream(provider_id=pid, media_type="movie", stream_key="1234",
                                      tmdb_id=1, title="Mislabelled"))
            self.db.add(MatchOverride(provider_id=pid, media_type="movie", stream_key="5678",
                                      tmdb_id=2, title="Fixed"))
        self.db.commit()
        # the other provider has the highest id, so delete it too for `old`'s id to be reused
        self._delete(other)
        self._delete(old)

        new = self._provider("New")
        self._same_id(old, new)
        self.db.expire_all()
        self.assertEqual({}, provider_health.load_state(self.db),
                         "a deleted provider's health state was kept for the next one with its id")
        self.assertEqual(0, self.db.query(BlockedStream).count(),
                         "a deleted provider's blocked streams were kept for the next one with its id")
        self.assertEqual(0, self.db.query(MatchOverride).count(),
                         "a deleted provider's match fixes were kept for the next one with its id")


if __name__ == "__main__":
    unittest.main()
