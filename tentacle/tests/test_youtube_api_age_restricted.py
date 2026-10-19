"""#276: with a Data API key, age-restricted uploads entered the library.

The API reports an age-restricted upload as public; only
contentDetails.contentRating.ytRating = "ytAgeRestricted" says otherwise, and
it was not read. yt-dlp can't play such a video without cookies ("Sign in to
confirm your age"), so every play was a 502, in every user's row. Without a key
the same video was skipped, because yt-dlp's details fail for it.

Run from tentacle/:  python -m unittest discover -s tests -p test_youtube_api_age_restricted.py
"""
import unittest
from unittest import mock

from services.youtube import feeds, indexer


def _item(rating=None, made_for_kids=False):
    content = {"duration": "PT10M"}
    if rating is not None:
        content["contentRating"] = rating
    return {"id": "abcdefghijk",
            "snippet": {"title": "t", "publishedAt": "2026-09-15T10:00:00Z", "liveBroadcastContent": "none"},
            "contentDetails": content,
            "status": {"privacyStatus": "public", "madeForKids": made_for_kids}}


class AgeRestrictedFromTheApi(unittest.TestCase):
    def _keep(self, item):
        ch = mock.Mock(live_enabled=False, include_streams=False, min_duration=0)
        return indexer._should_index(feeds.api_to_details(item), ch)

    def test_an_age_restricted_video_is_not_indexed(self):
        keep, reason = self._keep(_item({"ytRating": "ytAgeRestricted"}))
        self.assertFalse(keep)
        self.assertIn("needs_auth", reason)

    def test_an_ordinary_video_is_still_indexed(self):
        self.assertEqual((True, ""), self._keep(_item({})))
        self.assertEqual((True, ""), self._keep(_item()))

    def test_made_for_kids_is_unaffected(self):
        details = feeds.api_to_details(_item({}, made_for_kids=True))
        self.assertIs(True, details["is_made_for_kids"])
        self.assertIsNone(details["availability"])


if __name__ == "__main__":
    unittest.main()
