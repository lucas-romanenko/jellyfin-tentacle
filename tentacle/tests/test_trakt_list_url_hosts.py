"""A Trakt list pasted from another trakt.tv host is read from api.trakt.tv.

fetch_trakt_list() builds the API address with
url.replace('trakt.tv', 'api.trakt.tv') + '/items'. The list URL check allows
any trakt.tv subdomain, so a list copied from www.trakt.tv or app.trakt.tv is
accepted, and is then requested from www.api.trakt.tv / app.api.trakt.tv (no
such hosts: the list never fills). A URL with a query (?sort=...) gets
'/items' after the query.

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
        for host in ("www.trakt.tv", "app.trakt.tv"):
            with self.subTest(host=host):
                seen, items = self._requested(f"https://{host}/users/someone/lists/my-list")
                self.assertEqual(seen, [self.API])
                self.assertEqual([i["tmdb_id"] for i in items], [11])

    def test_query_string_is_not_part_of_the_api_path(self):
        seen, _ = self._requested("https://trakt.tv/users/someone/lists/my-list?sort=rank,asc")
        self.assertEqual(seen, [self.API])

    def test_api_host_pasted_directly(self):
        seen, _ = self._requested("https://api.trakt.tv/users/someone/lists/my-list")
        self.assertEqual(seen, [self.API])


if __name__ == "__main__":
    unittest.main()
