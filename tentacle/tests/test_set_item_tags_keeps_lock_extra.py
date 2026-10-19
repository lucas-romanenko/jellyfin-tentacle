"""A tag push or a rename keeps the item's lock and every other field (#161).

Run from the tentacle/ directory:  python -m unittest discover -s tests

test_set_item_tags_keeps_lock.py covers LockData on a tag push. These cover a
rename (set_item_name, the YouTube re-title of #131, builds its body the same
way) and the fields ItemUpdate would otherwise clear.
"""
import unittest

from services.jellyfin import _ECHOED_ITEM_FIELDS, JellyfinService


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
    def test_a_rename_keeps_the_lock_and_the_tags(self):
        jf = _service(LOCKED)
        self.assertTrue(jf.set_item_name("i1", "Heat (Director's Cut)"))
        payload = jf.session.posted[0][1]
        self.assertEqual("Heat (Director's Cut)", payload["Name"])
        self.assertIs(True, payload["LockData"])
        self.assertEqual(["Netflix Movies"], payload["Tags"])


# What one tag push cleared on Jellyfin 10.11.8, because ItemUpdate sets every
# field left out of the body to null (Rob's live check on #161).
MOVIE = {"Id": "m1", "Name": "Heat", "Tags": [], "LockData": False, "ProviderIds": {"Tmdb": "949"},
         "CustomRating": "Family", "CriticRating": 87, "ForcedSortName": "Heat 1995",
         "PreferredMetadataLanguage": "fr", "PreferredMetadataCountryCode": "CA",
         "AspectRatio": "2.39:1", "Video3DFormat": "HalfSideBySide", "ProductionLocations": ["USA"],
         "DateCreated": "2024-01-02T03:04:05.0000000Z"}
SERIES = {"Id": "s1", "Name": "Friends", "Tags": [], "LockData": True, "ProviderIds": {"Tmdb": "1668"},
          "Status": "Ended", "EndDate": "2004-05-06T00:00:00.0000000Z", "DisplayOrder": "dvd",
          "AirDays": ["Thursday"], "AirTime": "8:00 PM", "RunTimeTicks": 13200000000, "CustomRating": "TV-14"}


class NothingElseIsCleared(unittest.TestCase):
    def test_every_field_the_get_had_goes_back_unchanged(self):
        for item in (MOVIE, SERIES):
            jf = _service(item)
            jf.set_item_tags(item["Id"], ["IMDB TOP 250"])
            payload = jf.session.posted[0][1]
            for field, value in item.items():
                if field == "Tags":
                    continue
                self.assertEqual(value, payload.get(field), f"{item['Name']}: {field}")

    def test_a_field_the_get_did_not_have_is_left_out(self):
        jf = _service({"Id": "m2", "Name": "Plain", "Tags": [], "CriticRating": None})
        jf.set_item_tags("m2", ["x"])
        payload = jf.session.posted[0][1]
        for field in _ECHOED_ITEM_FIELDS:
            self.assertNotIn(field, payload)

    def test_a_rename_keeps_them_too(self):
        jf = _service(SERIES)
        jf.set_item_name("s1", "Friends (1994)")
        payload = jf.session.posted[0][1]
        self.assertEqual("Friends (1994)", payload["Name"])
        self.assertEqual("Ended", payload["Status"])
        self.assertEqual("dvd", payload["DisplayOrder"])


if __name__ == "__main__":
    unittest.main()
