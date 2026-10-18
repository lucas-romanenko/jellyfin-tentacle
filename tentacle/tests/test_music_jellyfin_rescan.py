"""After a Lidarr import, Tentacle's Jellyfin music rescan must make the album
show up WITH its tags.

JellyfinPlayer.rescan() posted /Items/{library}/Refresh?Recursive=true with
no MetadataRefreshMode. Jellyfin then adds the new album without reading its
files' tags: the album is named after its folder ("Album (1973)"), its tracks
after their files, with no MusicBrainz ids, and later library scans leave it
that way. Tentacle's "Open in Jellyfin" then says Jellyfin doesn't list the
album (it is looked up by its MusicBrainz release-group id).

And when the music library was created on an empty folder (Tentacle's
"Create library" on Lidarr's new root folder), Jellyfin has no folder under
the library yet: a refresh of the library finds nothing at all until a full
library scan, so imports never appear until Jellyfin's scheduled scan.

Run from tentacle/:  python -m unittest discover -s tests -p "test_music_jellyfin_rescan.py"
"""
import unittest
from unittest import mock

from web_stubs import _ensure_web_stubs

_ensure_web_stubs()


def _player():
    from services.music.players import JellyfinPlayer
    p = JellyfinPlayer.__new__(JellyfinPlayer)
    p.url, p.key, p.library_id, p.db = "http://jf", "k", "lib1", None
    return p


class TestJellyfinMusicRescan(unittest.TestCase):
    def _rescan(self, listing):
        p = _player()
        ok = mock.Mock(status_code=204)
        ok.raise_for_status = mock.Mock()
        with mock.patch.object(p, "_get", return_value=listing), \
             mock.patch("services.music.players.requests.post", return_value=ok) as post:
            p.rescan()
        return post

    def test_library_refresh_reads_the_new_files_tags(self):
        post = self._rescan({"Items": [{"Id": "x"}], "TotalRecordCount": 3})
        self.assertEqual(post.call_count, 1)
        self.assertEqual(post.call_args.args[0], "http://jf/Items/lib1/Refresh")
        params = post.call_args.kwargs["params"]
        self.assertEqual(params.get("Recursive"), "true")
        # Without these Jellyfin adds the album untagged, for good.
        self.assertEqual(params.get("MetadataRefreshMode"), "Default")
        self.assertEqual(params.get("ImageRefreshMode"), "Default")
        self.assertNotEqual(str(params.get("ReplaceAllMetadata", "false")).lower(), "true")

    def test_a_library_jellyfin_lists_nothing_under_gets_a_full_scan(self):
        post = self._rescan({"Items": [], "TotalRecordCount": 0})
        self.assertEqual(post.call_count, 1)
        self.assertEqual(post.call_args.args[0], "http://jf/Library/Refresh")

    def test_no_library_picked_does_nothing(self):
        p = _player()
        p.library_id = ""
        with mock.patch("services.music.players.requests.post") as post:
            p.rescan()
        post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
