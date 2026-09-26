"""Pushing tags must not unlock a Jellyfin item (#161).

Run from the tentacle/ directory:  python -m unittest discover -s tests

set_item_tags() builds a minimal ItemUpdate payload, and it had no LockData.
Jellyfin takes a missing LockData as false (checked on 10.11.8): every tag push,
after each sync and on "Refresh Tags", unlocked the item, and the next metadata
refresh could overwrite the edits the lock was protecting. set_item_name (the
YouTube re-title, #131) builds its payload the same way.
"""
import unittest

from services.jellyfin import JellyfinService


class _Resp:
    status_code = 204
    text = ""


class _Session:
    def __init__(self):
        self.posted = []

    def post(self, url, json=None, timeout=None):
        self.posted.append((url, json))
        return _Resp()


def _service(item):
    jf = JellyfinService("http://jf:8096", "k", "u1")
    jf.session = _Session()
    jf._get = lambda path, params=None: dict(item)
    return jf


LOCKED = {"Id": "i1", "Name": "Heat", "Tags": ["Netflix Movies"], "LockData": True,
          "LockedFields": ["Name", "Overview"], "ProviderIds": {"Tmdb": "949"}}


class KeepsTheLock(unittest.TestCase):
    def test_a_tag_push_keeps_lock_data_and_locked_fields(self):
        jf = _service(LOCKED)
        self.assertTrue(jf.set_item_tags("i1", ["Netflix Movies", "IMDB TOP 250"]))
        url, payload = jf.session.posted[0]
        self.assertTrue(url.endswith("/Items/i1"))
        self.assertIs(True, payload["LockData"])
        self.assertEqual(["Name", "Overview"], payload["LockedFields"])
        self.assertEqual(["Netflix Movies", "IMDB TOP 250"], payload["Tags"])
        self.assertEqual("Heat", payload["Name"])

    def test_an_unlocked_item_stays_unlocked(self):
        jf = _service({**LOCKED, "LockData": False, "LockedFields": []})
        jf.set_item_tags("i1", ["x"])
        self.assertIs(False, jf.session.posted[0][1]["LockData"])

    def test_a_rename_keeps_the_lock_and_the_tags(self):
        jf = _service(LOCKED)
        self.assertTrue(jf.set_item_name("i1", "Heat (Director's Cut)"))
        payload = jf.session.posted[0][1]
        self.assertEqual("Heat (Director's Cut)", payload["Name"])
        self.assertIs(True, payload["LockData"])
        self.assertEqual(["Netflix Movies"], payload["Tags"])


if __name__ == "__main__":
    unittest.main()
