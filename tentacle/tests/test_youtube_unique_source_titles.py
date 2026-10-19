"""Two YouTube sources must never share a title (follow-up to #169).

Once a playlist is titled by its own name (529955a), a playlist named like its
channel ("Bluey" on the Bluey channel) or two owners' "Favorites" get the same
title — and the title is a key: get_desired_smartlists() skips a second source
of the same name (so it silently has no Jellyfin playlist and no home row),
and removing a source removes the home rows whose display_name is its title.
add_channel now stores a unique title: "<title> (<owner>)" for a playlist,
then "(2)", "(3)". A source already stored is never renamed.

Run from tentacle/:  python -m unittest discover -s tests -p "test_youtube_unique_source_titles.py"
"""
import unittest
from unittest import mock

import test_youtube


class TestUniqueTitles(unittest.TestCase):
    setUp = test_youtube.TestAddingAChannel.setUp
    tearDown = test_youtube.TestAddingAChannel.tearDown
    _Req = test_youtube.TestAddingAChannel._Req
    _add = test_youtube.TestAddingAChannel._add

    def _channel(self, title, owner_id):
        self.info.update(kind="channel", playlist_id=None, channel_id=owner_id, title=title, owner=title,
                         canonical="https://www.youtube.com/@x")

    def _pl(self, pid, title, owner_id, owner):
        self.info.update(kind="playlist", playlist_id=pid, title=title, channel_id=owner_id, owner=owner,
                         canonical=f"https://www.youtube.com/playlist?list={pid}")

    def _titles(self):
        return sorted(r.title for r in self.db.query(self.YouTubeChannel))

    def _yt_playlists(self):
        from services.smartlists import get_desired_smartlists
        return sorted(s["name"] for s in get_desired_smartlists(self.db, user_id=1)
                      if str(s.get("tag", "")).startswith("yt:"))

    # ── collisions at add time ────────────────────────────────────────────
    def test_a_playlist_named_like_its_channel_gets_its_own_playlist(self):
        o = "UC" + "d" * 22
        self._channel("Bluey", o)
        self._add()
        self._pl("PLc", "Bluey", o, "Bluey")
        self._add()
        self.assertEqual(self._titles(), ["Bluey", "Bluey (2)"])
        self.assertEqual(self._yt_playlists(), ["Bluey", "Bluey (2)"])

    def test_two_owners_playlists_are_told_apart_by_owner(self):
        self._pl("PLa", "Favorites", "UC" + "b" * 22, "Ann")
        self._add()
        self._pl("PLb", "Favorites", "UC" + "c" * 22, "Bob")
        self._add()
        self.assertEqual(self._yt_playlists(), ["Favorites", "Favorites (Bob)"])

    def test_further_clashes_are_numbered_case_insensitively(self):
        self._pl("PL1", "Mix", "UC" + "e" * 22, "Mix")
        self._add()
        self._pl("PL2", "mix", "UC" + "e" * 22, "Mix")
        self._add()
        self._pl("PL3", "MIX", "UC" + "e" * 22, "Mix")
        self._add()
        self.assertEqual(self._titles(), ["MIX (3)", "Mix", "mix (2)"])

    def test_a_channel_whose_title_is_taken_is_numbered(self):
        self._pl("PLa", "Science", "UC" + "g" * 22, "Someone")
        self._add()
        self._channel("Science", "UC" + "h" * 22)
        self._add()
        self.assertEqual(self._titles(), ["Science", "Science (2)"])

    def test_distinct_titles_are_untouched(self):
        self._channel("Chan", "UC" + "f" * 22)
        self._add()
        self._pl("PLx", "Season 1", "UC" + "f" * 22, "Chan")
        self._add()
        self.assertEqual(self._titles(), ["Chan", "Season 1"])

    def test_the_same_playlist_twice_is_still_refused(self):
        from fastapi import HTTPException
        self._pl("PLa", "Favorites", "UC" + "b" * 22, "Ann")
        self._add()
        with self.assertRaises(HTTPException) as cm:
            self._add()
        self.assertEqual(cm.exception.status_code, 409)

    # ── existing installs ────────────────────────────────────────────────
    def test_a_stored_source_is_never_renamed(self):
        """Even two stored sources that already clash keep their titles; only the new one moves."""
        for slug in ("old-a", "old-b"):
            self.db.add(self.YouTubeChannel(input_url="u", kind="playlist", playlist_id=slug,
                                            title="Favorites", slug=slug, enabled=True))
        self.db.commit()
        self._pl("PLnew", "Favorites", "UC" + "b" * 22, "Ann")
        self._add()
        self.assertEqual(self._titles(), ["Favorites", "Favorites", "Favorites (Ann)"])

    # ── removal of one leaves the other's home row ───────────────────────
    def test_removing_one_leaves_the_others_home_row(self):
        from routers import youtube
        import routers.smartlists as rsl
        import services.smartlists as ssl
        import models.database as mdb
        o = "UC" + "d" * 22
        self._channel("Bluey", o)
        self._add()
        self._pl("PLc", "Bluey", o, "Bluey")
        self._add()
        chan = self.db.query(self.YouTubeChannel).filter_by(kind="channel").one()
        other = self.db.query(self.YouTubeChannel).filter_by(kind="playlist").one()
        kept = other.title
        # Rows are found by the playlist id Tentacle recorded for the removed
        # source, never by name (#52), so the rows and smartlists carry ids.
        home = {"rows": [{"playlist_id": "pl-bluey", "display_name": "Bluey", "order": 1},
                         {"playlist_id": "pl-kept", "display_name": kept, "order": 2}],
                "hero": {"enabled": True, "playlist_id": "pl-kept", "display_name": kept}}
        smartlists = [
            {"playlist_id": "pl-bluey", "name": "Bluey", "is_youtube": True, "yt_tags": [f"yt:{chan.slug}"]},
            {"playlist_id": "pl-kept", "name": kept, "is_youtube": True, "yt_tags": [f"yt:{other.slug}"]},
        ]
        written = {}
        with mock.patch.object(mdb, "SessionLocal", return_value=self.db), \
             mock.patch.object(rsl, "_read_home_json", side_effect=lambda u: {
                 "rows": [dict(r) for r in home["rows"]], "hero": dict(home["hero"])}), \
             mock.patch.object(rsl, "_write_home_json", side_effect=lambda u, c: written.update(c)), \
             mock.patch.object(ssl, "sync_smartlists"), mock.patch.object(ssl, "write_home_config"), \
             mock.patch.object(ssl, "_notify_jellyfin_plugin"), \
             mock.patch.object(ssl, "_get_smartlists_with_playlist_ids", return_value=smartlists):
            youtube._cleanup_after_remove("Bluey", False, slug=chan.slug)
        self.assertEqual([r["display_name"] for r in written["rows"]], [kept])
        self.assertTrue(written.get("hero", {}).get("enabled", True))


if __name__ == "__main__":
    unittest.main()
