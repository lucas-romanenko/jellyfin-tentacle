"""A list refresh that could not read the whole list never deletes what it missed
(#145, #163, #164, #160).

Run from the tentacle/ directory:  python -m unittest discover -s tests

Every fetcher returned a bare list, so a partial read was stored as the whole
list: what it missed was deleted from list_items and lost its tag.
- #145: IMDb's GraphQL answers 403, the fallback (Servarr) sees movies only, so
  every refresh deleted the list's TV shows; a failure on page 2+ kept page 1 only.
- #163: a TMDB 429 on an IMDb-only item read as "no such title".
- #164: a Letterboxd film page that failed, or a challenge page, ended the read.
- #160: no Trakt client id was an ERROR for every list, every night.
Drives the real refresh_list(); only the network is faked.
"""
import logging
import shutil
import unittest
from unittest import mock

import requests
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb
import routers.lists as lists
from models.database import ListItem, ListSubscription, Movie, Series, TentacleUser
from tmp_dirs import temp_dir


def setUpModule():
    logging.disable(logging.CRITICAL)


def tearDownModule():
    logging.disable(logging.NOTSET)


class _Resp:
    def __init__(self, status=200, json_data=None, text=""):
        self.status_code, self._json, self.text = status, json_data, text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} Error")

    def json(self):
        return self._json


def _gql_page(items, has_next=False, cursor=None):
    edges = [{"node": {"item": {"id": imdb, "titleText": {"text": t},
                                "titleType": {"id": "tvSeries" if kind == "series" else "movie"},
                                "releaseYear": {"year": 2000}}}} for imdb, t, kind in items]
    return _Resp(json_data={"data": {"list": {"items": {
        "edges": edges, "pageInfo": {"hasNextPage": has_next, "endCursor": cursor}}}}})


class FakeTMDB:
    """IMDb id -> TMDB metadata; ids in `fail` answer like a 429."""

    def __init__(self, table, fail=()):
        self.table, self.fail, self._failed = table, set(fail), False

    def find_by_imdb_id(self, imdb_id):
        self._failed = imdb_id in self.fail
        return None if self._failed else self.table.get(imdb_id)

    def lookup_failed(self):
        return self._failed

    def get_movie_details(self, tid):
        return None

    get_series_details = get_movie_details


def _refresh(lst, db, **kw):
    """refresh_list()'s answer as these tests read it (it returns items, stats)."""
    items, _stats = lists.refresh_list(lst, db, **kw)
    db.commit()
    return {"ok": items is not None, "note": lst.last_fetch_note,
            "complete": items is not None and getattr(items, "complete", True)}


TMDB_TABLE = {"tt1": {"tmdb_id": 1, "title": "Film 1", "media_type": "movie"},
              "tt2": {"tmdb_id": 2, "title": "Film 2", "media_type": "movie"},
              "tt3": {"tmdb_id": 3, "title": "Show 3", "media_type": "series"},
              "tt4": {"tmdb_id": 4, "title": "Show 4", "media_type": "series"}}


class _Db(unittest.TestCase):
    def setUp(self):
        self.tmp = temp_dir(self)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        engine = create_engine(f"sqlite:///{self.tmp}/t.db")
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)
        self.user = TentacleUser(jellyfin_user_id="a" * 32, display_name="A", is_admin=True)
        self.db.add(self.user)
        self.db.commit()

    def make_list(self, type_, url, stored):
        lst = ListSubscription(user_id=self.user.id, name="L", type=type_, url=url, tag="My List", active=True)
        self.db.add(lst)
        self.db.commit()
        for tmdb_id, media_type in stored:
            self.db.add(ListItem(list_id=lst.id, tmdb_id=tmdb_id, media_type=media_type))
            model = Movie if media_type == "movie" else Series
            self.db.add(model(tmdb_id=tmdb_id, title=f"T{tmdb_id}", source="provider_1", tags=["My List"]))
        self.db.commit()
        return lst

    def stored(self, lst):
        self.db.expire_all()
        return sorted((i.tmdb_id, i.media_type) for i in self.db.query(ListItem).filter_by(list_id=lst.id))

    def tagged(self):
        self.db.expire_all()
        rows = self.db.query(Movie).all() + self.db.query(Series).all()
        return sorted(r.tmdb_id for r in rows if "My List" in (r.tags or []))


class ImdbLists(_Db):
    URL = "https://www.imdb.com/list/ls055592025/"

    def test_the_movies_only_fallback_keeps_the_lists_tv_shows(self):
        lst = self.make_list("imdb_rss", self.URL, [(1, "movie"), (2, "movie"), (3, "series")])
        servarr = [{"ImdbId": "tt1", "TmdbId": 1, "Title": "Film 1"}]   # film 2 left the list
        with mock.patch.object(lists.requests, "post", return_value=_Resp(403, text="<html>Forbidden")), \
                mock.patch.object(lists.requests, "get", return_value=_Resp(json_data=servarr)):
            result = _refresh(lst, self.db, tmdb=FakeTMDB(TMDB_TABLE))
        self.assertEqual([(1, "movie"), (3, "series")], self.stored(lst))
        self.assertEqual([1, 3], self.tagged())
        self.assertIn("TV shows unavailable", result["note"])
        self.assertIn("TV shows unavailable", lst.last_fetch_note)

    def test_a_later_page_failure_keeps_everything_after_it(self):
        lst = self.make_list("imdb_rss", self.URL, [(1, "movie"), (2, "movie"), (3, "series"), (4, "series")])
        pages = [_gql_page([("tt1", "Film 1", "movie")], has_next=True, cursor="c1"),
                 requests.ConnectionError("reset")]

        def post(*a, **k):
            nxt = pages.pop(0)
            if isinstance(nxt, Exception):
                raise nxt
            return nxt
        with mock.patch.object(lists.requests, "post", side_effect=post):
            result = _refresh(lst, self.db, tmdb=FakeTMDB(TMDB_TABLE))
        self.assertEqual([(1, "movie"), (2, "movie"), (3, "series"), (4, "series")], self.stored(lst))
        self.assertEqual([1, 2, 3, 4], self.tagged())
        self.assertFalse(result["complete"])
        self.assertIn("IMDb stopped answering", result["note"])

    def test_a_tmdb_429_does_not_drop_the_item(self):
        lst = self.make_list("imdb_rss", self.URL, [(1, "movie"), (2, "movie")])
        page = _gql_page([("tt1", "Film 1", "movie"), ("tt2", "Film 2", "movie")])
        with mock.patch.object(lists.requests, "post", return_value=page):
            result = _refresh(lst, self.db, tmdb=FakeTMDB(TMDB_TABLE, fail={"tt2"}))
        self.assertEqual([(1, "movie"), (2, "movie")], self.stored(lst))
        self.assertEqual([1, 2], self.tagged())
        self.assertIn("TMDB did not answer for 1 item", result["note"])

    def test_a_complete_read_still_removes_what_left(self):
        lst = self.make_list("imdb_rss", self.URL, [(1, "movie"), (2, "movie")])
        with mock.patch.object(lists.requests, "post", return_value=_gql_page([("tt1", "Film 1", "movie")])):
            result = _refresh(lst, self.db, tmdb=FakeTMDB(TMDB_TABLE))
        self.assertEqual([(1, "movie")], self.stored(lst))
        self.assertEqual([1], self.tagged())
        self.assertIsNone(result["note"])
        self.assertIsNone(lst.last_fetch_note)

    def test_an_address_that_is_not_an_imdb_list_is_refused(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as cm:
            lists.create_list(lists.ListCreate(name="https://www.imdb.com/list/ls1/", type="imdb_rss",
                                               url="My favourites", tag="Faves"), db=self.db, user=self.user)
        self.assertIn("Not an IMDb", cm.exception.detail)


class LetterboxdLists(_Db):
    URL = "https://letterboxd.com/someone/list/best/"

    def _page(self, slugs, next_page=False):
        html = "".join(f'<div data-target-link="/film/{s}/"></div>' for s in slugs)
        if next_page:
            html += '<a href="/someone/list/best/page/2/">next</a>'
        return _Resp(text=html)

    def _film(self, tmdb_id):
        return _Resp(text=f'<div data-tmdb-id="{tmdb_id}" data-tmdb-type="movie"></div>')

    def _run(self, answers):
        lst = self.make_list("letterboxd", self.URL, [(1, "movie"), (2, "movie"), (3, "movie")])

        def get(url, timeout=None, **k):
            for key, answer in answers.items():
                if key in url:
                    if isinstance(answer, Exception):
                        raise answer
                    return answer
            return _Resp(404)
        with mock.patch.object(requests.Session, "get", side_effect=lambda url, **k: get(url, **k)), \
                mock.patch.object(lists, "is_safe_url", lambda *a, **k: True):
            result = _refresh(lst, self.db)
        return lst, result

    def test_a_film_page_that_failed_keeps_that_film(self):
        lst, result = self._run({"/page/1/": self._page(["a", "b", "c"]), "/film/a/": self._film(1),
                                 "/film/b/": self._film(2), "/film/c/": _Resp(429)})
        self.assertEqual([(1, "movie"), (2, "movie"), (3, "movie")], self.stored(lst))
        self.assertIn("1 film page(s) did not load", result["note"])

    def test_a_challenge_page_is_not_the_end_of_the_list(self):
        lst, result = self._run({"/page/1/": self._page(["a"], next_page=True),
                                 "/page/2/": _Resp(text="<html><title>Just a moment...</title></html>"),
                                 "/film/a/": self._film(1)})
        self.assertEqual([(1, "movie"), (2, "movie"), (3, "movie")], self.stored(lst))
        self.assertIn("page 2 of the list did not load", result["note"])

    def test_a_film_page_served_as_a_challenge_keeps_that_film(self):
        lst, result = self._run({"/page/1/": self._page(["a", "b", "c"]), "/film/a/": self._film(1),
                                 "/film/b/": self._film(2),
                                 "/film/c/": _Resp(text="<html><title>Just a moment...</title></html>")})
        self.assertEqual([(1, "movie"), (2, "movie"), (3, "movie")], self.stored(lst))
        self.assertIn("1 film page(s) did not load", result["note"])


class TraktLists(_Db):
    def test_no_client_id_keeps_the_list_warns_once_and_says_so(self):
        lst = self.make_list("trakt", "https://trakt.tv/users/x/lists/y", [(1, "movie")])
        lists._trakt_unconfigured_logged = False
        with self.assertLogs("routers.lists", level="WARNING") as logs:
            logging.disable(logging.NOTSET)
            try:
                first = _refresh(lst, self.db, trakt_client_id="")
                _refresh(lst, self.db, trakt_client_id="")
            finally:
                logging.disable(logging.CRITICAL)
        self.assertEqual(1, sum("Trakt client ID not configured" in m for m in logs.output))
        self.assertFalse(any(m.startswith("ERROR") for m in logs.output))
        self.assertEqual([(1, "movie")], self.stored(lst))
        self.assertFalse(first["ok"])
        self.assertIn("no Trakt client ID", lst.last_fetch_note)


class TmdbFindReportsFailures(unittest.TestCase):
    """The real TMDBService: a 429 on /find is "unknown", not "no such title"."""

    def _service(self, status):
        from services.tmdb import TMDBService
        tmp = temp_dir(self)
        self.addCleanup(shutil.rmtree, tmp, True)
        svc = TMDBService(bearer_token="x", cache_dir=tmp)

        def get(url, params=None, timeout=None):
            r = requests.Response()
            r.status_code = status
            r._content = b'{"movie_results": [], "tv_results": []}'
            r.url = url
            return r
        svc.session.get = get
        return svc

    def test_a_429_is_reported_and_not_cached(self):
        svc = self._service(429)
        self.assertIsNone(svc.find_by_imdb_id("tt0133093"))
        self.assertTrue(svc.lookup_failed())
        ok = self._service(200)
        ok.cache_dir = svc.cache_dir
        self.assertIsNone(ok.find_by_imdb_id("tt0133093"))
        self.assertFalse(ok.lookup_failed(), "a real 'not found' is not a failure")

    def test_the_flag_resets_per_lookup(self):
        svc = self._service(429)
        svc.find_by_imdb_id("tt1")
        svc.session.get = self._service(200).session.get
        svc.find_by_imdb_id("tt2")
        self.assertFalse(svc.lookup_failed())


class ListCardsSeeTheNote(_Db):
    def test_get_lists_returns_the_note(self):
        lst = self.make_list("trakt", "https://trakt.tv/users/x/lists/y", [])
        lst.last_fetch_note = "TV shows unavailable"
        self.db.commit()
        self.assertEqual("TV shows unavailable", lists.get_lists(db=self.db, user=self.user)[0]["last_fetch_note"])


if __name__ == "__main__":
    unittest.main()
