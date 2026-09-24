"""The server says which cards a client may preview on focus (all / local
files only / off), and the plugin hands that to every client with the
toolbar config.

Run from the tentacle/ directory:  python -m unittest discover -s tests

Why: the Android TV app starts a server transcode of a focused card's item
after 500 ms. For a provider (.strm) title that is a provider connection per
card scrolled over -- measured: four in fourteen seconds on one row, each
lingering after focus moved on -- and on a connection-limited account that
is what cut a running recording off (androidtv#47).
"""
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb

try:
    import routers.smartlists as rsl
except Exception:  # pragma: no cover
    rsl = None

PLUGIN = Path(__file__).resolve().parents[2] / "tentacle-plugin"


def _db():
    engine = create_engine(f"sqlite:///{tempfile.mkdtemp()}/t.db")
    mdb.Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    user = mdb.TentacleUser(id=1, jellyfin_user_id="jf-1", display_name="User 1")
    db.add(user)
    db.commit()
    return db, user


@unittest.skipIf(rsl is None, "fastapi not installed")
class Endpoint(unittest.TestCase):
    def _call(self, mode, existing=None):
        db, user = _db()
        written = {}
        with mock.patch.object(rsl, "_read_home_json", lambda u: dict(existing or {})), \
                mock.patch.object(rsl, "_write_home_json", lambda u, c: written.update(c)), \
                mock.patch.object(rsl, "_notify_jellyfin_plugin", lambda d: None), \
                mock.patch.object(rsl, "bump_playlist_version", lambda: None):
            out = rsl.set_card_previews(rsl.CardPreviewsRequest(mode=mode), db, user)
        return out, written

    def test_each_policy_is_stored_and_the_rest_of_the_config_kept(self):
        for mode in ("all", "local_only", "off"):
            out, written = self._call(mode, existing={"rows": [{"x": 1}], "merge_continue_watching": True})
            self.assertEqual({"success": True, "card_previews": mode}, out)
            self.assertEqual(mode, written["card_previews"])
            self.assertEqual([{"x": 1}], written["rows"])
            self.assertTrue(written["merge_continue_watching"])

    def test_a_missing_config_gets_a_minimal_one(self):
        out, written = self._call("off", existing=None)
        self.assertEqual("off", written["card_previews"])
        self.assertIn("rows", written)

    def test_anything_else_is_rejected(self):
        from fastapi import HTTPException
        for bad in ("", "sometimes", "ALL "):
            if bad.strip().lower() in rsl.CARD_PREVIEW_POLICIES:
                continue
            with self.assertRaises(HTTPException) as cm:
                self._call(bad)
            self.assertEqual(422, cm.exception.status_code)

    def test_case_and_whitespace_are_forgiven(self):
        out, written = self._call(" Local_Only ")
        self.assertEqual("local_only", written["card_previews"])


class SurvivesRegeneration(unittest.TestCase):
    def test_write_home_config_carries_the_policy_over(self):
        src = (Path(__file__).resolve().parents[1] / "services" / "smartlists.py").read_text(encoding="utf-8")
        self.assertIn('config["card_previews"] = existing_config["card_previews"]', src,
                      "the home config is rebuilt on every sync; the policy must be carried over like merge_continue_watching")


class PluginHandsItToClients(unittest.TestCase):
    def test_home_config_model_reads_it(self):
        cs = (PLUGIN / "HomeScreen" / "HomeScreenManager.cs").read_text(encoding="utf-8")
        self.assertRegex(cs, r'\[JsonPropertyName\("card_previews"\)\]\s*public string\? CardPreviews')

    def test_toolbar_and_sections_expose_it(self):
        cs = (PLUGIN / "Api" / "HomeScreenController.cs").read_text(encoding="utf-8")
        toolbar = cs[cs.index('[HttpGet("Toolbar")]'):]
        toolbar = toolbar[:toolbar.index("[HttpGet(", 10)]
        self.assertEqual(2, len(re.findall(r"cardPreviews", toolbar)) - 1,
                         "both Toolbar returns carry cardPreviews")
        self.assertIn('? "all" :', toolbar, "absent = all, so older configs change nothing")
        self.assertIn("cardPreviews = string.IsNullOrEmpty(config.CardPreviews)", cs)

    def test_dashboard_offers_the_three_choices(self):
        html = (Path(__file__).resolve().parents[1] / "static" / "index.html").read_text(encoding="utf-8")
        js = (Path(__file__).resolve().parents[1] / "static" / "js" / "pages.js").read_text(encoding="utf-8")
        self.assertIn('id="card-previews-select"', html)
        for v in ("all", "local_only", "off"):
            self.assertIn(f'<option value="{v}">', html)
        self.assertIn("/api/smartlists/card-previews", js)
        self.assertIn("config.card_previews", js)


if __name__ == "__main__":
    unittest.main()
