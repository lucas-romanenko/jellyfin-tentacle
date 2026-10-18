"""A provider that refuses the login is said every night, not only for two (#267).

An Xtream panel that refuses the account (wrong or expired login) answers every
action with {"user_info": {"auth": 0}}. The client read that as an empty
category, so the first nights failed with "it returned no titles", and from
the third night on (EMPTY_CATEGORY_STRIKES) the categories were believed
emptied and the run was "completed" with no message again. Runs the real
sync_provider with the real XtreamClient over a stubbed HTTP session.
"""
import json
import unittest

import services.sync as sync
from models.database import Movie, ProviderCategory
from nightly_harness import NightlyHarness

REFUSED = '{"user_info": {"auth": 0}}'


class _Resp:
    status_code = 200

    def __init__(self, body):
        self.text = body
        self.content = body.encode()
        self.headers = {"Content-Type": "application/json"}

    def raise_for_status(self):
        pass

    def json(self):
        return json.loads(self.text)


class _Session:
    """Stands in for XtreamClient.session: answer(url) gives the body."""
    headers = {}

    def __init__(self, answer):
        self.answer = answer

    def get(self, url, *a, **k):
        return _Resp(self.answer(url))

    def mount(self, *a, **k):
        pass

    def close(self):
        pass


def _catalogue(url):
    if "get_vod_streams" in url:
        return json.dumps([{"name": "Heat (2010)", "stream_id": 1000, "container_extension": "mp4"}])
    if "get_series_info" in url:
        return json.dumps({"episodes": {"1": [{"id": 50001, "episode_num": 1, "container_extension": "mp4"}]}})
    if "get_series" in url:
        return json.dumps([{"name": "Friends (2010)", "series_id": 5000}])
    return "[]"


class RefusedLogin(NightlyHarness):
    def setUp(self):
        super().setUp()
        self.add_category("1")
        self.add_category("s1", type_="series")
        self.catalogue_movies("1", ["Heat"])
        self.catalogue_series("s1", ["Friends"])
        self.night()                                    # a library, synced
        self.assertIsNotNone(self.movie(1000))

    def _answer(self, answer):
        client = sync.XtreamClient(self.provider)
        client.session = _Session(answer)
        sync.make_provider_client = lambda p: client

    def _sync(self):
        run = sync.sync_provider(self.provider, "full", self.db)
        self.db.expire_all()
        return run

    def _strikes(self):
        return {c.category_id: c.consecutive_empty_syncs or 0 for c in self.db.query(ProviderCategory).all()}

    def test_every_refused_night_fails_with_the_reason_and_renewal_recovers(self):
        self._answer(lambda url: REFUSED)
        for night in range(1, 6):
            with self.subTest(night=night):
                run = self._sync()
                self.assertEqual("failed", run.status, run.error_message)
                self.assertIn("refused the login", run.error_message)
                self.assertEqual(1, self.db.query(Movie).count())       # nothing pruned
                self.assertEqual({"1": 0, "s1": 0}, self._strikes())     # no "emptied" strikes
        self._answer(_catalogue)                                          # account renewed
        run = self._sync()
        self.assertEqual("completed", run.status, run.error_message)
        self.assertIsNone(run.error_message)
        self.assertEqual({"1": 0, "s1": 0}, self._strikes())
        self.assertEqual(1, self.db.query(Movie).count())

    def test_an_expired_status_gives_the_same_reason(self):
        self._answer(lambda url: '{"user_info": {"auth": 0, "status": "Expired"}}')
        run = self._sync()
        self.assertEqual("failed", run.status)
        self.assertIn("refused the login", run.error_message)

    def test_a_night_refused_for_some_categories_completes_with_a_warning(self):
        self._answer(lambda url: REFUSED if "get_series&" in url else _catalogue(url))
        run = self._sync()
        self.assertEqual("completed", run.status)
        self.assertIn("1 of 2 categories could not be read", run.error_message)
        self.assertIn("refused the login", run.error_message)
        self.assertEqual(1, self.db.query(Movie).count())
        self.assertEqual({"1": 0, "s1": 0}, self._strikes())

    def test_other_objects_still_read_as_an_empty_list(self):
        client = sync.XtreamClient(self.provider)
        for body in ("{}", '{"user_info": {"auth": 1}}', '{"user_info": "x"}', "[]"):
            with self.subTest(body=body):
                client.session = _Session(lambda url, b=body: b)
                self.assertEqual([], client.get_vod_streams("1"))


if __name__ == "__main__":
    unittest.main()
