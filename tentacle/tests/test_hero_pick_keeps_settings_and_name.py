"""Picking a hero playlist keeps the hero's settings and names the playlist (#650).

1. POST /api/smartlists/hero rebuilt the hero from a fixed key list without
   "trailer_audio": a user who turned the trailer sound on (hero-sort) and then
   picked another playlist got it dropped, and the plugin served
   trailerAudio=false. The dashboard read a missing key as "on" (and its
   checkbox started checked), so it showed sound on while clients played muted.
2. A playlist that is neither a home row nor one of Tentacle's own (one the
   user made in Jellyfin, passed through by the plugin's POST /TentacleHome/Hero)
   was stored with its id as display_name, and clients showed the 32-hex id.
   Jellyfin knows the name; it is asked as that user, so a playlist the user
   can't see is not named.

A fake Jellyfin runs on loopback. Based on therobmilne's test in the issue.
Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import json
import re
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlparse

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import routers.smartlists as rsl
from models.database import Base, Setting, TentacleUser
from tmp_dirs import temp_dir

STATIC = Path(__file__).resolve().parents[1] / "static"
USER = "a" * 32
OLD, TENTACLE_PL, FAMILY, SECRET = "1" * 32, "2" * 32, "3" * 32, "4" * 32


class _FakeJellyfin(BaseHTTPRequestHandler):
    """Answers item reads. "Family picks" is the user's; "Secret" belongs to
    someone else, so only an unscoped (admin) read sees it."""
    items = {FAMILY: ("Family picks", {USER}), SECRET: ("Secret", set())}

    def log_message(self, *a):
        pass

    def do_GET(self):
        u = urlparse(self.path)
        q = {k.lower(): v[0] for k, v in parse_qs(u.query).items()}
        m = re.fullmatch(r"(?:/Users/(\w+))?/Items/(\w+)", u.path)
        found = None
        if m and m.group(2) in self.items:
            name, visible_to = self.items[m.group(2)]
            as_user = (m.group(1) or q.get("userid", "")).replace("-", "")
            if not as_user or as_user in visible_to:
                found = {"Id": m.group(2), "Name": name, "Type": "Playlist"}
        body = json.dumps(found or {}).encode()
        self.send_response(200 if found else 404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        self.send_response(204)
        self.end_headers()


class HeroPick(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(temp_dir(self))
        srv = HTTPServer(("127.0.0.1", 0), _FakeJellyfin)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        engine = create_engine(f"sqlite:///{self.tmp}/t.db", connect_args={"check_same_thread": False})
        Base.metadata.create_all(engine)
        self.addCleanup(engine.dispose)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)
        for k, v in (("jellyfin_url", f"http://127.0.0.1:{srv.server_port}"),
                     ("jellyfin_api_key", "k" * 32)):
            self.db.add(Setting(key=k, value=v))
        self.user = TentacleUser(jellyfin_user_id=USER, display_name="User", is_admin=False)
        self.db.add(self.user)
        self.db.commit()
        for name, value in (("HOME_CONFIG_DIR", str(self.tmp / "home-configs")),
                            ("bump_playlist_version", lambda: None),
                            ("_notify_jellyfin_plugin", lambda db: {"notified": False}),
                            ("_get_smartlists_with_playlist_ids",
                             lambda db, user_id=None: [{"playlist_id": TENTACLE_PL, "name": "New Movies"}])):
            p = mock.patch.object(rsl, name, value)
            p.start()
            self.addCleanup(p.stop)

    def _pick(self, playlist_id, hero=None):
        if hero is not None:
            rsl._write_home_json(self.user, {"rows": [], "hero": hero})
        out = rsl.set_hero(rsl.HeroPickRequest(playlist_id=playlist_id), self.db, self.user)
        self.assertEqual({"success": True}, out)
        return rsl._read_home_json(self.user)["hero"]

    def test_picking_another_playlist_keeps_trailer_audio(self):
        hero = self._pick(TENTACLE_PL, {
            "enabled": True, "playlist_id": OLD, "display_name": "Old", "sort_by": "random",
            "sort_order": "Descending", "require_logo": True, "require_trailer": True,
            "trailer_audio": True, "item_count": 15})
        self.assertEqual("New Movies", hero["display_name"])
        self.assertIs(True, hero.get("trailer_audio"), f"trailer_audio dropped: {hero}")
        self.assertIs(True, hero["require_trailer"])
        self.assertEqual(15, hero["item_count"])

    def test_a_first_pick_states_the_trailer_audio_clients_use(self):
        # The plugin's default is muted; the stored hero says so, not a missing key.
        hero = self._pick(TENTACLE_PL)
        self.assertIs(False, hero.get("trailer_audio"), hero)

    def test_a_jellyfin_playlist_gets_its_jellyfin_name_not_its_id(self):
        hero = self._pick(FAMILY)
        self.assertEqual(FAMILY, hero["playlist_id"])
        self.assertEqual("Family picks", hero["display_name"])

    def test_a_playlist_the_user_cannot_see_is_not_named(self):
        hero = self._pick(SECRET)
        self.assertNotEqual("Secret", hero["display_name"])
        self.assertEqual(SECRET, hero["display_name"])

    def test_jellyfin_down_still_saves_the_pick_under_its_id(self):
        set_ = self.db.query(Setting).filter(Setting.key == "jellyfin_url").one()
        set_.value = "http://127.0.0.1:9"
        self.db.commit()
        hero = self._pick(FAMILY)
        self.assertEqual(FAMILY, hero["display_name"])

    def test_turning_the_hero_off_still_resets_it(self):
        hero = self._pick("", {"enabled": True, "playlist_id": OLD, "display_name": "Old",
                               "trailer_audio": True})
        self.assertFalse(hero["enabled"])
        self.assertEqual("", hero["playlist_id"])


class DashboardTrailerAudio(unittest.TestCase):
    """The dashboard shows the same default as the clients: off unless set."""

    def test_checkbox_reads_a_missing_setting_as_off(self):
        js = (STATIC / "js" / "pages.js").read_text()
        self.assertFalse("config.hero.trailer_audio !== false" in js, "a missing trailer_audio reads as on")
        self.assertTrue("config.hero.trailer_audio === true" in js, "trailer_audio is not read as off by default")
        self.assertFalse("getElementById('home-hero-trailer-audio')?.checked ?? true" in js,
                         "updateHeroSort falls back to sound on")

    def test_checkbox_does_not_start_checked(self):
        html = (STATIC / "index.html").read_text()
        tag = re.search(r'<input[^>]*id="home-hero-trailer-audio"[^>]*>', html).group(0)
        self.assertNotRegex(tag, r"\bchecked\b")


if __name__ == "__main__":
    unittest.main()
