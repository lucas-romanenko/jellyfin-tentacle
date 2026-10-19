"""A tag push must not clear the item's other fields (#161, beyond LockData).

Jellyfin's ItemUpdate treats the body as a full replacement: a field the body
leaves out is cleared. set_item_tags' minimal body carried Name, Overview,
Genres, Tags, Studios, People, ProviderIds, dates and ratings — nothing else —
so one push (checked live on Jellyfin 10.11.8) cleared a movie's
CriticRating, CustomRating, ForcedSortName and PreferredMetadataLanguage, and
a series' Status, EndDate, DisplayOrder and CustomRating. CustomRating feeds
parental ratings. Fields are now sent back as read, and only when set.

Run from tentacle/:  python -m unittest discover -s tests -p "test_set_item_tags_keeps_fields.py"
"""
import unittest
from unittest import mock

MOVIE = {"Id": "m1", "Name": "The Matrix", "Tags": ["Netflix Movies"], "LockData": False,
         "CriticRating": 88, "CustomRating": "KIDS-CUSTOM", "ForcedSortName": "Matrix 1",
         "PreferredMetadataLanguage": "lt", "PreferredMetadataCountryCode": "LT",
         "RunTimeTicks": 81600000000, "DateCreated": "2026-01-02T03:04:05.0000000Z"}
SERIES = {"Id": "s1", "Name": "Cheers", "Tags": [], "LockData": True,
          "Status": "Ended", "EndDate": "1993-05-20T00:00:00.0000000Z", "DisplayOrder": "absolute",
          "CustomRating": "TV-KIDS", "AirDays": ["Thursday"], "AirTime": "9:00 PM"}


def _sent(item, call):
    from services.jellyfin import JellyfinService
    jf = JellyfinService("http://jf.invalid:8096", "k", "u1")
    jf.session = mock.Mock()
    jf.session.post.return_value = mock.Mock(status_code=204, text="")
    with mock.patch.object(jf, "_get", return_value=dict(item)):
        assert call(jf)
    return jf.session.post.call_args.kwargs["json"]


class TestMovieFieldsSurvive(unittest.TestCase):
    def test_ratings_sort_name_and_metadata_language(self):
        sent = _sent(MOVIE, lambda jf: jf.set_item_tags("m1", ["Netflix Movies", "Watchlist"]))
        for f in ("CriticRating", "CustomRating", "ForcedSortName", "PreferredMetadataLanguage",
                  "PreferredMetadataCountryCode", "RunTimeTicks", "DateCreated"):
            with self.subTest(field=f):
                self.assertEqual(sent.get(f), MOVIE[f])
        self.assertEqual(sent["Tags"], ["Netflix Movies", "Watchlist"])


class TestSeriesFieldsSurvive(unittest.TestCase):
    def test_status_end_date_display_order_and_custom_rating(self):
        sent = _sent(SERIES, lambda jf: jf.set_item_tags("s1", ["Netflix TV"]))
        for f in ("Status", "EndDate", "DisplayOrder", "CustomRating", "AirDays", "AirTime"):
            with self.subTest(field=f):
                self.assertEqual(sent.get(f), SERIES[f])
        self.assertIs(sent["LockData"], True)


class TestPayloadStaysSmall(unittest.TestCase):
    def test_fields_the_item_does_not_have_are_not_sent(self):
        sent = _sent({"Id": "m2", "Name": "Plain", "Tags": []}, lambda jf: jf.set_item_tags("m2", ["x"]))
        for f in ("CriticRating", "CustomRating", "ForcedSortName", "Status", "EndDate",
                  "DisplayOrder", "LockedFields"):
            with self.subTest(field=f):
                self.assertNotIn(f, sent)

    def test_a_rename_keeps_them_too(self):
        sent = _sent(MOVIE, lambda jf: jf.set_item_name("m1", "New name"))
        self.assertEqual(sent["Name"], "New name")
        self.assertEqual(sent["CustomRating"], "KIDS-CUSTOM")
        self.assertEqual(sent["Tags"], ["Netflix Movies"])


if __name__ == "__main__":
    unittest.main()
