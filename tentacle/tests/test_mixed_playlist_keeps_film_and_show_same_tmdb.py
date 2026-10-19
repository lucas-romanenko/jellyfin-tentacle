"""A playlist of films AND shows must keep a film and a show that share a TMDB number.

Run from the tentacle/ directory:  python -m unittest discover -s tests

TMDB numbers films and shows separately (movie/1396 and tv/1396 are different
titles). services/smartlists._process_single_playlist_locked de-duplicates the
query result by ProviderIds["Tmdb"] alone ("same content in two libraries"), so
in a mixed playlist (a tag rule on "both" -- the default --, a list with films
and shows, "<user>'s Downloads") one of the two silently never gets in.
"""
import unittest

from tmp_dirs import temp_dir
from pathlib import Path


class FakeJF:
    def __init__(self, items):
        self.items = items
        self.created_with = None

    def query_items(self, **kw):
        return list(self.items)

    def item_exists(self, pid):
        return False

    def create_playlist(self, name, item_ids=None, user_id=None, is_public=False):
        self.created_with = list(item_ids or [])
        return "PL1"


class MixedPlaylistSameTmdb(unittest.TestCase):
    def test_film_and_show_with_one_tmdb_number_both_listed(self):
        from services.smartlists import _build_config, _process_single_playlist_locked
        folder = Path(temp_dir(self))
        config = _build_config("Picks", "Picks", ["Movie", "Series"], "f1", True, "u1")
        jf = FakeJF([
            {"Id": "film", "Type": "Movie", "Name": "A Film", "ProviderIds": {"Tmdb": "1396"}},
            {"Id": "show", "Type": "Series", "Name": "A Show", "ProviderIds": {"Tmdb": "1396"}},
        ])
        stats = {"processed": 0, "created": 0, "updated": 0, "changed": 0, "errors": 0, "item_counts": {}}
        _process_single_playlist_locked(jf, folder, config, "u1", stats, db=None)
        self.assertEqual(["film", "show"], jf.created_with,
                         "a show was dropped from a mixed playlist because a film has its TMDB number")

    def test_same_film_in_two_libraries_still_once(self):
        """What the de-duplication is for keeps working."""
        from services.smartlists import _build_config, _process_single_playlist_locked
        folder = Path(temp_dir(self))
        config = _build_config("Picks", "Picks", ["Movie", "Series"], "f1", True, "u1")
        jf = FakeJF([
            {"Id": "film", "Type": "Movie", "ProviderIds": {"Tmdb": "1396"}},
            {"Id": "film4k", "Type": "Movie", "ProviderIds": {"Tmdb": "1396"}},
        ])
        stats = {"processed": 0, "created": 0, "updated": 0, "changed": 0, "errors": 0, "item_counts": {}}
        _process_single_playlist_locked(jf, folder, config, "u1", stats, db=None)
        self.assertEqual(["film"], jf.created_with)


if __name__ == "__main__":
    unittest.main()
