"""A Letterboxd list read only in part must not lose the films it missed.

fetch_letterboxd_rss() reads the list pages, then each film's page (ten at a
time) for its TMDB id. A film page that failed — a 429 is likely at that rate
— was logged at debug level and dropped, and a list page that rendered no
films (Cloudflare answers its challenge with a 200) ended the list there. The
refresh then stored the rest as the whole list and stripped the list's tag
from every film it had missed.

HTML fixtures carry only what the parser reads (data-target-link, the
page-link check, data-tmdb-id/type and og:title).
Run from tentacle/:  python -m unittest discover -s tests -p "test_letterboxd_partial.py"
"""
import unittest
from unittest import mock

from test_imdb_partial_list import _fresh_db

URL = "https://letterboxd.com/rob/list/favourites"


class _Page:
    def __init__(self, status=200, text=""):
        self.status_code = status
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(str(self.status_code), response=self)


def _list_page(slugs, next_page=None):
    body = "".join(f'<div data-target-link="/film/{s}/"></div>' for s in slugs)
    if next_page:
        body += f'<a href="/rob/list/favourites/page/{next_page}/">next</a>'
    return _Page(200, body)


def _film(tmdb_id):
    return _Page(200, f'<div data-tmdb-id="{tmdb_id}" data-tmdb-type="movie"></div>'
                      f'<meta property="og:title" content="Film {tmdb_id}">')


class _Session:
    def __init__(self, pages):
        self.pages = pages
        self.headers = {}

    def get(self, url, timeout=None):
        return self.pages(url)


class TestLetterboxdPartial(unittest.TestCase):
    def setUp(self):
        from models.database import ListItem, ListSubscription, Movie, TentacleUser
        self.db = _fresh_db()
        self.user = TentacleUser(jellyfin_user_id="u1", display_name="Rob")
        self.db.add(self.user)
        self.db.commit()
        self.lst = ListSubscription(user_id=self.user.id, name="Favourites", type="letterboxd",
                                    tag="Favourites", url=URL)
        self.db.add(self.lst)
        self.db.commit()
        for tmdb_id in (1, 2, 3):
            self.db.add(ListItem(list_id=self.lst.id, tmdb_id=tmdb_id, media_type="movie",
                                 title=f"Film {tmdb_id}"))
            self.db.add(Movie(tmdb_id=tmdb_id, title=f"Film {tmdb_id}", source="provider_1",
                              tags=["Favourites"]))
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def _fetch(self, pages):
        from routers import lists
        with mock.patch.object(lists.requests, "Session", return_value=_Session(pages)), \
             mock.patch.object(lists, "is_safe_url", return_value=True), \
             mock.patch.object(lists, "_get_tmdb_service", return_value=None), \
             mock.patch("services.smartlists._notify_jellyfin_plugin"):
            return lists.fetch_list(self.lst.id, db=self.db, user=self.user)

    def _stored(self):
        from models.database import ListItem
        return sorted(r.tmdb_id for r in self.db.query(ListItem).filter(ListItem.list_id == self.lst.id))

    def _tagged(self):
        from models.database import Movie
        return sorted(m.tmdb_id for m in self.db.query(Movie) if "Favourites" in (m.tags or []))

    def test_a_film_page_that_fails_keeps_its_film(self):
        def pages(url):
            if "/page/1/" in url:
                return _list_page(["film-1", "film-2", "film-3"])
            if "film-3" in url:
                return _Page(429)
            return _film(int(url.rstrip("/").rsplit("-", 1)[1]))
        self._fetch(pages)
        self.assertEqual(self._stored(), [1, 2, 3])
        self.assertEqual(self._tagged(), [1, 2, 3])
        self.db.refresh(self.lst)
        self.assertIn("1 film page(s) did not load", self.lst.last_fetch_note or "")

    def test_a_list_page_that_does_not_render_keeps_the_rest(self):
        def pages(url):
            if "/page/1/" in url:
                return _list_page(["film-1"], next_page=2)
            if "/page/2/" in url:
                return _Page(200, "<html>Just a moment...</html>")
            return _film(int(url.rstrip("/").rsplit("-", 1)[1]))
        self._fetch(pages)
        self.assertEqual(self._stored(), [1, 2, 3])
        self.assertEqual(self._tagged(), [1, 2, 3])

    def test_a_complete_read_still_removes_films(self):
        def pages(url):
            if "/page/1/" in url:
                return _list_page(["film-1", "film-2"])
            return _film(int(url.rstrip("/").rsplit("-", 1)[1]))
        self._fetch(pages)
        self.assertEqual(self._stored(), [1, 2])
        self.assertEqual(self._tagged(), [1, 2])
        self.db.refresh(self.lst)
        self.assertIsNone(self.lst.last_fetch_note)


if __name__ == "__main__":
    unittest.main()
