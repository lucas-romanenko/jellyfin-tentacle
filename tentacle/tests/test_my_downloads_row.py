""""My Downloads" in everyone's row picker (Lucas, 2026-11-09, after giving
his dad Roman an account: "why does he not have an option to add a downloads
row to the home screen to see his downloads ... each person should get their
own my downloads row option").

The row is the person's own "<name>'s Downloads" playlist (what they asked
Radarr/Sonarr for that is in the library, newest first). Picking "My
Downloads" switches it on, before their first download too.

Run from the tentacle/ directory:  python -m unittest tests.test_my_downloads_row
"""
import unittest
from unittest import mock

import models.database as mdb
import routers.smartlists as rsl
from test_default_home import Base as HomeBase


class MyDownloadsRow(HomeBase):
    def setUp(self):
        super().setUp()
        self.toggled = []

        def toggle(db, user_id, key, enabled):
            self.toggled.append((user_id, key, enabled))
            self.built[user_id] = [
                {"name": a["name"], "playlist_id": f"{user_id}-{a['key']}", "sort_by": "datecreated",
                 "sort_order": "Descending", "is_youtube": False}
                for a in rsl._compute_auto_playlists(db, user_id=user_id) if a.get("enabled")]
            return {"success": True}

        p = mock.patch.object(rsl, "toggle_auto_playlist_fast", toggle)
        p.start()
        self.addCleanup(p.stop)
        # Ethan has his own home with one row
        self.write(2, {"hero": {"enabled": False, "playlist_id": "", "display_name": ""},
                       "rows": [{"type": "builtin", "section_id": "resumevideo",
                                 "display_name": "Continue Watching", "order": 1}]})

    def sections(self, uid, scope=None):
        return [s["section_id"] for s in rsl.list_builtin_sections(db=self.db, user=self.user(uid), scope=scope)["sections"]]

    def test_offered_to_everyone_before_their_first_download(self):
        self.assertEqual(self.db.query(mdb.DownloadRequest).count(), 0)
        for uid in (1, 2, 3, 4):
            self.assertIn("my_downloads", self.sections(uid), f"person {uid}")

    def test_picking_it_makes_their_playlist_and_adds_the_row(self):
        out = rsl.add_row(rsl.AddRowRequest(section_id="my_downloads"), db=self.db, user=self.user(2), scope=None)
        self.assertTrue(out["success"], out)
        self.assertEqual(self.toggled, [(2, "builtin:my_downloads", True)])
        toggle = self.db.query(mdb.AutoPlaylistToggle).filter_by(user_id=2, key="builtin:my_downloads").one()
        self.assertTrue(toggle.enabled)
        row = sorted(self.cfg(2)["rows"], key=lambda r: r["order"])[0]
        self.assertEqual((row["type"], row["playlist_id"], row["display_name"]),
                         ("playlist", "2-builtin:my_downloads", "ethan's Downloads"))
        self.assertEqual((row["sort_by"], row["sort_order"]), ("datecreated", "Descending"), "newest first")
        self.assertEqual(row["shape"], "poster")
        self.assertIn(self.user(2), self.notified, "open clients are told")
        self.assertNotIn("my_downloads", self.sections(2), "on the home now")

    def test_not_offered_twice(self):
        """Their downloads playlist isn't also listed among the playlists."""
        self.db.add(mdb.AutoPlaylistToggle(user_id=2, key="builtin:my_downloads", enabled=True))
        self.db.commit()
        self.built[2] = [{"name": "ethan's Downloads", "playlist_id": "pl-dl", "is_youtube": False}]
        names = [p["name"] for p in rsl.available_playlists(db=self.db, user=self.user(2), scope=None)["playlists"]]
        self.assertNotIn("ethan's Downloads", names)
        self.assertIn("my_downloads", self.sections(2))
        out = rsl.add_row(rsl.AddRowRequest(section_id="my_downloads"), db=self.db, user=self.user(2), scope=None)
        self.assertTrue(out["success"], out)
        self.assertEqual(self.toggled, [], "the playlist was there already")
        self.assertEqual(sorted(self.cfg(2)["rows"], key=lambda r: r["order"])[0]["playlist_id"], "pl-dl")

    def test_kids_get_it_too(self):
        out = rsl.add_row(rsl.AddRowRequest(section_id="my_downloads"), db=self.db, user=self.user(4), scope=None)
        self.assertTrue(out["success"], out)
        self.assertIn("mia's Downloads", self.names(4))

    def test_the_default_home_offers_the_ready_made_one_instead(self):
        self.assertNotIn("my_downloads", self.sections(1, scope="default"))
        names = [p["name"] for p in rsl.available_playlists(db=self.db, user=self.user(1), scope="default")["playlists"]]
        self.assertIn("My Downloads", names)

    def test_a_default_with_my_downloads_gives_everyone_their_own(self):
        rsl.add_row(rsl.AddRowRequest(playlist_id="builtin:my_downloads"), db=self.db, user=self.user(1), scope="default")
        self.assertIn("fahad's Downloads", self.names(3), "fahad follows the default, no downloads yet")

    def test_failure_says_so(self):
        def broken(db, user_id, key, enabled):
            return {"error": "Jellyfin not configured"}
        with mock.patch.object(rsl, "toggle_auto_playlist_fast", broken):
            out = rsl.add_row(rsl.AddRowRequest(section_id="my_downloads"), db=self.db, user=self.user(2), scope=None)
        self.assertFalse(out["success"])
        self.assertIn("Jellyfin not configured", out["message"])


if __name__ == "__main__":
    unittest.main()
