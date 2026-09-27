"""Pushing tags must not unlock a Jellyfin item.

JellyfinService.set_item_tags() posts a minimal ItemUpdate built from a GET of
the item. It left LockData out, and Jellyfin 10.11.8 treats a missing LockData
as false: checked live, an item locked in the Jellyfin UI ("Lock this item")
came back LockData=False after one tag push. The push runs for every tagged
movie and series after each sync, so a user's lock never survived the night,
and the next metadata refresh could replace the edits it protected.

Run from tentacle/:  python -m unittest discover -s tests -p "test_set_item_tags_keeps_lock.py"
"""
import unittest
from unittest import mock


class TestSetItemTagsKeepsTheLock(unittest.TestCase):
    def _push(self, item):
        from services.jellyfin import JellyfinService
        jf = JellyfinService("http://jf.invalid:8096", "k", "u1")
        jf.session = mock.Mock()
        jf.session.post.return_value = mock.Mock(status_code=204, text="")
        with mock.patch.object(jf, "_get", return_value=item):
            self.assertTrue(jf.set_item_tags(item["Id"], ["Netflix Movies"]))
        return jf.session.post.call_args.kwargs["json"]

    def test_a_locked_item_stays_locked(self):
        sent = self._push({"Id": "m1", "Name": "The Matrix", "Tags": [], "LockData": True})
        self.assertIs(sent["LockData"], True)
        self.assertEqual(sent["Tags"], ["Netflix Movies"])

    def test_an_unlocked_item_stays_unlocked(self):
        sent = self._push({"Id": "m1", "Name": "The Matrix", "Tags": [], "LockData": False})
        self.assertIs(sent["LockData"], False)


if __name__ == "__main__":
    unittest.main()
