"""A Trakt list pasted from any trakt.tv address is read from api.trakt.tv (#681).

fetch_trakt_list() built the API address with a string replace of "trakt.tv"
by "api.trakt.tv". The list URL check accepts every trakt.tv subdomain, so a
list pasted from www.trakt.tv or app.trakt.tv was saved and then requested
from www.api.trakt.tv / app.api.trakt.tv (no such hosts), one pasted from
api.trakt.tv from api.api.trakt.tv, and a ?sort= query ended up inside the
path. The list card said "Trakt did not answer" on every refresh.

Run from tentacle/:  python -m unittest discover -s tests -p "test_trakt_list_url_hosts.py"
"""
import unittest
from unittest import mock


class _Resp:
    status_code = 200

    def raise_for_status(self):
        pass

    def json(self):
        return [{"type": "movie", "movie": {"title": "A Film", "ids": {"tmdb": 11}}}]


class TestTraktListUrlHosts(unittest.TestCase):
    API = "https://api.trakt.tv/users/someone/lists/my-list/items"

    def _requested(self, url):
        from routers import lists
        seen = []

        def fake_get(u, *a, **k):
            seen.append(u)
            return _Resp()
        with mock.patch.object(lists, "is_safe_url", return_value=True), \
             mock.patch.object(lists.requests, "get", side_effect=fake_get):
            items = lists.fetch_trakt_list(url, client_id="cid")
        return seen, items

    def test_plain_host_still_works(self):
        seen, items = self._requested("https://trakt.tv/users/someone/lists/my-list")
        self.assertEqual(seen, [self.API])
        self.assertEqual([i["tmdb_id"] for i in items], [11])

    def test_www_and_app_hosts_go_to_the_api_host(self):
        for host in ("www.trakt.tv", "app.trakt.tv", "WWW.Trakt.TV"):
            with self.subTest(host=host):
                seen, items = self._requested(f"https://{host}/users/someone/lists/my-list")
                self.assertEqual(seen, [self.API])
                self.assertEqual([i["tmdb_id"] for i in items], [11])

    def test_api_host_pasted_directly(self):
        for url in ("https://api.trakt.tv/users/someone/lists/my-list",
                    "https://api.trakt.tv/users/someone/lists/my-list/items"):
            with self.subTest(url=url):
                seen, _ = self._requested(url)
                self.assertEqual(seen, [self.API])

    def test_query_fragment_and_trailing_slash_are_not_part_of_the_api_path(self):
        for url in ("https://trakt.tv/users/someone/lists/my-list?sort=rank,asc",
                    "https://trakt.tv/users/someone/lists/my-list/?sort=rank,asc",
                    "https://trakt.tv/users/someone/lists/my-list#top",
                    "https://app.trakt.tv/users/someone/lists/my-list/"):
            with self.subTest(url=url):
                seen, _ = self._requested(url)
                self.assertEqual(seen, [self.API])


if __name__ == "__main__":
    unittest.main()
