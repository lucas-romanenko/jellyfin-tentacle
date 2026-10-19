"""Discover detail gives a Jellyfin item id only to a caller Jellyfin shows it to.

Run from the tentacle/ directory:  python -m unittest discover -s tests

The detail's jellyfin_item_id (what Play / Open use) comes from a map built
as the configured Jellyfin user, and was returned to every caller. For a
profile Jellyfin hides the item from (parental rating, blocked tags, library
access), the web page and the TV app already fail to open it (they look it
up as that user), but the id was still in the reply. The id is now
confirmed as the caller (GET /Items/{id}?userId=<caller>, 404 = hidden) and
left out when Jellyfin hides the item or can't answer; in_library is
unchanged. The configured user
skips the check; answers are cached per caller and item for 10 minutes.
"""
import unittest
from unittest import mock

import requests

import test_discover_library as _lib
from test_discover_library import JF_URL, FakeJellyfin, movie
import routers.discover as discover
from models.database import Movie


def _resp(code):
    r = requests.Response()
    r.status_code = code
    return r


class CallerScopedItemId(_lib.TestDiscoverDetail):
    def as_caller(self, jf_id):
        p = mock.patch.object(discover, "get_user_from_request",
                              lambda request, db: mock.Mock(jellyfin_user_id=jf_id, is_admin=False, id=2))
        p.start()
        self.addCleanup(p.stop)
        discover._visible_cache.clear()
        self.addCleanup(discover._visible_cache.clear)
        discover._visible_down_until[0] = 0
        self.addCleanup(discover._visible_down_until.__setitem__, 0, 0)

    def setUp(self):
        super().setUp()
        self.add_row(Movie, 603, jellyfin_item_id="item-1")
        self.use_jellyfin(FakeJellyfin({"Movie": [movie(603, "item-1")]}))

    def test_hidden_from_the_caller(self):
        self.as_caller("restricted")
        with mock.patch("requests.get", return_value=_resp(404)) as get:
            d = self.detail("movie", 603)
        self.assertIsNone(d["jellyfin_item_id"])
        self.assertNotIn("jellyfin_url", d)
        self.assertTrue(d["in_library"], "in library as before; only the id is left out")
        self.assertEqual(f"{JF_URL}/Items/item-1", get.call_args.args[0])
        self.assertEqual({"userId": "restricted"}, get.call_args.kwargs["params"])
        self.assertEqual("item-1", self.stored_id(Movie, 603), "the stored id is not changed")

    def test_visible_to_the_caller_and_cached(self):
        self.as_caller("other")
        with mock.patch("requests.get", return_value=_resp(200)) as get:
            self.assertEqual("item-1", self.detail("movie", 603)["jellyfin_item_id"])
            self.assertEqual("item-1", self.detail("movie", 603)["jellyfin_item_id"])
        self.assertEqual(1, get.call_count)

    def test_unknown_answer_leaves_it_out_and_is_not_cached(self):
        self.as_caller("other")
        with mock.patch("requests.get", return_value=_resp(503)):
            self.assertIsNone(self.detail("movie", 603)["jellyfin_item_id"])
        with mock.patch("requests.get", return_value=_resp(200)):
            self.assertEqual("item-1", self.detail("movie", 603)["jellyfin_item_id"])

    def test_after_a_connection_failure_jellyfin_is_not_waited_on_again_for_a_while(self):
        self.as_caller("other")
        with mock.patch("requests.get", side_effect=requests.ConnectionError("down")) as get:
            self.assertIsNone(self.detail("movie", 603)["jellyfin_item_id"])
            self.assertIsNone(self.detail("movie", 603)["jellyfin_item_id"])
        self.assertEqual(1, get.call_count, "every click waited on an unreachable Jellyfin")
        discover._visible_down_until[0] = 0  # the pause is over
        with mock.patch("requests.get", return_value=_resp(200)):
            self.assertEqual("item-1", self.detail("movie", 603)["jellyfin_item_id"])

    def test_the_configured_user_is_not_asked_again(self):
        with mock.patch("requests.get") as get:
            self.assertEqual("item-1", self.detail("movie", 603)["jellyfin_item_id"])
        get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
