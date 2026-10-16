"""Deleting a provider must not leave its per-provider Live TV settings for
the next provider to inherit.

Run from the tentacle/ directory:  python -m unittest discover -s tests

Live TV keeps some state in settings keyed by the provider's id
(livetv_m3u_switched_off_<id> from #158, livetv_epg_coverage_<id>,
livetv_epg_synced_<id>, livetv_output_formats_<id>). delete_provider() removes
the provider's rows but not these keys, and SQLite hands the next provider the
same id when the deleted one had the highest id (the table has no
AUTOINCREMENT). The pending address rewrite (provider_relink_pending, #149)
is keyed the same way. So a provider deleted and set up again -- the usual answer to
"my provider gave me a new address" -- starts with the old one's state: a
channel the user had once switched off on the OLD provider arrives switched
off in an enabled group on the new one, and the coverage report shows the old
provider's numbers until the first guide sync.
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
from models.database import LiveChannel, LiveChannelGroup, get_setting, set_setting
from routers import providers
from services.m3u_parser import parse_m3u
from tmp_dirs import temp_dir

KEYS = ("livetv_m3u_switched_off_{}", "livetv_epg_coverage_{}", "livetv_epg_synced_{}",
        "livetv_output_formats_{}")


def setUpModule():
    logging.disable(logging.CRITICAL)


def tearDownModule():
    logging.disable(logging.NOTSET)


class ProviderDeleteForgetsLiveSettings(unittest.TestCase):
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

    def test_a_new_provider_does_not_inherit_the_deleted_ones_state(self):
        lineup = [f"Channel {i}" for i in range(30)]
        old = self._provider("Old address")
        self._sync(old, lineup)
        ch = self._channels(old)["Channel 3"]
        ch.enabled = False                                   # the user's own "off"
        self.db.commit()
        self._sync(old, [n for n in lineup if n != "Channel 3"])  # the old provider drops it
        self.assertEqual(["Channel 3"], livetv._m3u_switched_off(self.db, old))
        set_setting(self.db, f"livetv_epg_coverage_{old}", '{"enabled": 30, "with_guide": 30}')
        # an address change saved but not rewritten yet (#149), plus another provider's
        set_setting(self.db, "provider_relink_pending", json.dumps({str(old): {"m3u": True},
                                                                    "99": {"m3u": True}}))
        self.db.commit()

        with mock.patch("services.smartlists.sync_smartlists"), \
                mock.patch("services.smartlists.refresh_smartlist_playlists"), \
                mock.patch("services.smartlists.write_home_config"):
            providers.delete_provider(old, db=self.db)

        new = self._provider("New address")
        if new != old:
            self.skipTest("the database did not reuse the id")
        leftover = [key.format(new) for key in KEYS if get_setting(self.db, key.format(new), "")]
        self._sync(new, lineup)
        self.assertTrue(self._channels(new)["Channel 3"].enabled,
                        "a channel switched off on the deleted provider arrived switched off on the new one")
        self.assertEqual([], leftover, "settings outlived the provider they belonged to")
        self.assertEqual({"99": {"m3u": True}},
                         json.loads(get_setting(self.db, "provider_relink_pending", "") or "{}"),
                         "the deleted provider's pending rewrite was kept (or another one's dropped)")


if __name__ == "__main__":
    unittest.main()
