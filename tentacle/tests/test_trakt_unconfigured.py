"""A Trakt list with no client ID configured is a setup gap, not an ERROR (A18).

fetch_trakt_list() logged "Trakt client ID not configured" at ERROR on every
refresh of every Trakt list — each night — and the list card said nothing.
Now: one WARNING per process, and the reason on the list card.

Run from tentacle/:  python -m unittest discover -s tests -p "test_trakt_unconfigured.py"
"""
import logging
import unittest
from unittest import mock

from test_imdb_partial_list import _fresh_db


class TestTraktWithoutClientId(unittest.TestCase):
    def setUp(self):
        from models.database import ListSubscription, TentacleUser
        from routers import lists
        lists._trakt_unconfigured_logged = False
        self.db = _fresh_db()
        self.user = TentacleUser(jellyfin_user_id="u", display_name="Rob")
        self.db.add(self.user)
        self.db.commit()
        self.lists = [ListSubscription(user_id=self.user.id, name=f"T{i}", type="trakt", tag=f"T{i}",
                                       url=f"https://trakt.tv/users/rob/lists/l{i}") for i in range(2)]
        self.db.add_all(self.lists)
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def test_one_warning_no_error_and_the_card_says_why(self):
        from routers import lists
        with self.assertLogs("routers.lists", level="WARNING") as logs, \
             mock.patch.object(lists, "_get_tmdb_service", return_value=None), \
             mock.patch.object(lists.requests, "get", side_effect=AssertionError("no request")):
            for _ in range(2):
                for lst in self.lists:
                    lists.refresh_list(lst, self.db)
        self.assertFalse([r for r in logs.records if r.levelno >= logging.ERROR])
        self.assertEqual(sum("Trakt client ID" in r.getMessage() for r in logs.records), 1)
        self.assertIn("no Trakt client ID", self.lists[0].last_fetch_note)


    def test_the_fetch_itself_logs_no_error(self):
        from routers import lists
        with self.assertLogs("routers.lists", level="DEBUG") as logs:
            logging.getLogger("routers.lists").debug("start")
            lists.fetch_trakt_list("https://trakt.tv/users/rob/lists/l0", client_id="")
            lists.fetch_trakt_list("https://trakt.tv/users/rob/lists/l1", client_id="")
        self.assertEqual([r.getMessage() for r in logs.records if r.levelno >= logging.ERROR], [])


if __name__ == "__main__":
    unittest.main()
