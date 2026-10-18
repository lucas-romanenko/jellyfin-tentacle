"""A list Fetch must bring Jellyfin's tags in line, not only the NFOs (#679).

Jellyfin keeps tags that were set through its API (Radarr/Sonarr scans push
them that way). A Fetch only rewrites NFOs, so a title that left the list
keeps the tag in Jellyfin, and stays in the list's playlist and home row,
until the nightly sync or Refresh Tags. The rename path already pushes just
the changed titles with sync_owned_tags(..., only=...).

This test fetches a list from which one of two titles has gone, and expects
sync_owned_tags to be asked about that title.

Run from tentacle/:  python -m unittest discover -s tests -p "test_list_fetch_pushes_tags.py"
"""
import time
import unittest
from unittest import mock

from test_imdb_partial_list import _fresh_db


class TestListFetchPushesTags(unittest.TestCase):
    def setUp(self):
        from models.database import ListItem, ListSubscription, Movie, TentacleUser, set_setting
        self.db = _fresh_db()
        self.user = TentacleUser(jellyfin_user_id="u1", display_name="Viewer")
        self.db.add(self.user)
        self.db.commit()
        self.lst = ListSubscription(user_id=self.user.id, name="L", type="trakt", tag="My List",
                                    url="https://trakt.tv/users/someone/lists/l")
        self.db.add(self.lst)
        self.db.commit()
        for t in (101, 102):
            self.db.add(Movie(tmdb_id=t, title=f"Film {t}", year="2001", source="radarr", tags=["My List"]))
            self.db.add(ListItem(list_id=self.lst.id, tmdb_id=t, media_type="movie", title=f"Film {t}"))
        set_setting(self.db, "jellyfin_url", "http://jf")
        set_setting(self.db, "jellyfin_api_key", "k")
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def test_a_title_that_left_the_list_is_pushed_to_jellyfin(self):
        from routers import lists
        from routers.lists import ListFetch
        fetched = ListFetch([{"tmdb_id": 101, "title": "Film 101", "media_type": "movie"}])
        push = mock.Mock(return_value={"written": 1, "unchanged": 0, "not_found": 0, "errors": 0})
        patches = [mock.patch.object(lists, "fetch_list_tmdb_ids", return_value=fetched),
                   mock.patch.object(lists, "_get_tmdb_service", return_value=None),
                   mock.patch("services.jellyfin.sync_owned_tags", push),
                   mock.patch("services.jellyfin.JellyfinService"),
                   mock.patch("services.smartlists._notify_jellyfin_plugin"),
                   # whatever refreshes the owner's playlists afterwards stays inert here
                   mock.patch("services.smartlists.sync_smartlists"),
                   mock.patch("services.smartlists.refresh_smartlist_playlists"),
                   mock.patch("services.smartlists.write_home_config")]
        if hasattr(lists, "sync_owned_tags"):
            patches.append(mock.patch.object(lists, "sync_owned_tags", push))
        for p in patches:
            p.start()
        try:
            lists.fetch_list(self.lst.id, db=self.db, user=self.user)
            for _ in range(50):          # a fix may push from a background thread
                if push.called:
                    break
                time.sleep(0.1)
        finally:
            for p in patches:
                p.stop()
        from models.database import Movie
        self.assertNotIn("My List", self.db.query(Movie).filter(Movie.tmdb_id == 102).one().tags or [])
        self.assertTrue(push.called, "the Fetch never pushed the changed tags to Jellyfin")
        only = push.call_args.kwargs.get("only") or {}
        self.assertIn(102, set(only.get("Movie") or ()))


if __name__ == "__main__":
    unittest.main()
