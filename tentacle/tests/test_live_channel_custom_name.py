"""A live channel can be renamed, and the rename survives channel syncs.

Provider channel names are long ("US: TORONTO MAPLE LEAFS"), and every channel
sync rewrote `name` from the provider, so there was no way to give a channel a
short, readable name in Jellyfin's guide. `custom_name` is the user's own name:
syncs never touch it, and the lineup, the M3U and the XMLTV all show it.

Run from tentacle/:  python -m unittest tests.test_live_channel_custom_name
"""
import tempfile
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb
import routers.livetv as livetv_router
from routers.auth import require_admin

PROVIDER_NAME = "US: TORONTO MAPLE LEAFS"


class _Client:
    def live_stream_url(self, sid):
        return f"http://192.0.2.10/live/u/p/{sid}.ts"


class CustomChannelName(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        engine = create_engine(f"sqlite:///{self.tmp}/t.db")
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        provider = mdb.Provider(name="P", server_url="http://192.0.2.10", username="u", password="p")
        self.db.add(provider)
        self.db.commit()
        self.provider_id = provider.id
        self.ch = mdb.LiveChannel(provider_id=provider.id, name=PROVIDER_NAME, stream_id="604267",
                                  stream_url="http://192.0.2.10/live/u/p/604267.ts",
                                  epg_channel_id="leafs.us", enabled=True)
        self.db.add(self.ch)
        self.db.commit()
        app = FastAPI()
        app.include_router(livetv_router.router)
        app.dependency_overrides[mdb.get_db] = lambda: self.db
        app.dependency_overrides[require_admin] = lambda: None
        self.client = TestClient(app)

    def _rename(self, name):
        return self.client.put(f"/api/live/channels/{self.ch.id}", json={"custom_name": name})

    def _sync(self):
        livetv_router._upsert_channels(self.provider_id, [{"stream_id": 604267, "name": PROVIDER_NAME,
                                                           "category_id": "1"}],
                                       {"1": "US| NHL TEAM PPV"}, _Client(), self.db)
        self.db.commit()

    def test_a_rename_reaches_the_lineup_the_m3u_and_the_xmltv(self):
        self.assertEqual(200, self._rename("NHL TOR").status_code)
        lineup = self.client.get("/hdhr/lineup.json").json()
        self.assertEqual(["NHL TOR"], [e["GuideName"] for e in lineup if e["GuideNumber"] == "604267"])
        self.assertIn(",NHL TOR\n", self.client.get("/api/live/playlist.m3u").text + "\n")
        self.assertIn("<display-name>NHL TOR</display-name>", self.client.get("/hdhr/xmltv.xml").text)

    def test_a_channel_sync_keeps_the_rename(self):
        self._rename("NHL TOR")
        self._sync()
        lineup = self.client.get("/hdhr/lineup.json").json()
        self.assertEqual(["NHL TOR"], [e["GuideName"] for e in lineup if e["GuideNumber"] == "604267"],
                         "the next channel sync put the provider's name back")

    def test_blank_puts_the_provider_name_back(self):
        self._rename("NHL TOR")
        self._rename("   ")
        lineup = self.client.get("/hdhr/lineup.json").json()
        self.assertEqual([PROVIDER_NAME], [e["GuideName"] for e in lineup if e["GuideNumber"] == "604267"])

    def test_the_channel_list_shows_both_names_and_search_finds_either(self):
        self._rename("NHL TOR")
        for term in ("NHL TOR", "MAPLE"):
            rows = self.client.get("/api/live/channels", params={"search": term}).json()["channels"]
            self.assertEqual([("NHL TOR", PROVIDER_NAME)], [(r["name"], r["provider_name"]) for r in rows], term)

    def test_an_overlong_name_is_refused(self):
        self.assertEqual(400, self._rename("X" * 65).status_code)


if __name__ == "__main__":
    unittest.main()
