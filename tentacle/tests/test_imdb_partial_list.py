"""An IMDb list that could only be read in part must not lose the rest (#145).

graphql.imdb.com answers 403 to Tentacle. _fetch_imdb_graphql_list() logged
that and returned nothing, fetch_imdb_rss() fell back to the Servarr mirror —
which only knows movies — and the refresh then stored that movies-only answer
as the whole list: every TV show was deleted from list_items and had the
list's tag stripped from its NFO, and nothing told the user. A failure on
page 2 or later did the same to everything after page 1.

No network: requests.post (GraphQL) and requests.get (Servarr) are stubbed
with responses shaped like the ones the parsers read, and TMDB is a stand-in.
Run from tentacle/:  python -m unittest discover -s tests -p "test_imdb_partial_list.py"
"""
import unittest
from pathlib import Path
from unittest import mock

import requests
from tmp_dirs import temp_dir


def _fresh_db():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    import models.database as mdb
    engine = create_engine(f"sqlite:///{temp_dir()}/t.db")
    mdb.Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


class _Resp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} Client Error: Forbidden", response=self)

    def json(self):
        return self._payload


def _gql_page(nodes, next_cursor=None):
    """One page of the list(id).items GraphQL answer, as the parser reads it."""
    return {"data": {"list": {"items": {
        "total": 999,
        "edges": [{"node": {"item": {
            "id": imdb_id, "titleText": {"text": title},
            "titleType": {"id": ttype}, "releaseYear": {"year": 2020},
        }}, "cursor": imdb_id} for imdb_id, title, ttype in nodes],
        "pageInfo": {"hasNextPage": bool(next_cursor), "endCursor": next_cursor},
    }}}}


# The Servarr mirror: movies only, with TMDB ids already resolved.
SERVARR_MOVIES = [
    {"ImdbId": "tt0000101", "TmdbId": 101, "Title": "Movie One", "Year": 2020},
    {"ImdbId": "tt0000102", "TmdbId": 102, "Title": "Movie Two", "Year": 2021},
]


class _FakeTMDB:
    """find_by_imdb_id for the GraphQL path: tt…NNN -> tmdb NNN."""
    TYPES = {"tt0000101": "movie", "tt0000102": "movie",
             "tt0000201": "series", "tt0000202": "series"}

    def find_by_imdb_id(self, imdb_id):
        return {"tmdb_id": int(imdb_id[-3:]), "title": imdb_id, "year": 2020,
                "poster_path": None, "media_type": self.TYPES.get(imdb_id, "movie")}

    def get_movie_details(self, tid):
        return None

    get_series_details = get_movie_details


class _ListCase(unittest.TestCase):
    TAG = "Top Lithuania"

    def setUp(self):
        from models.database import (ListItem, ListSubscription, Movie, Series,
                                     TentacleUser)
        self.db = _fresh_db()
        self.tmp = Path(temp_dir(self))
        self.user = TentacleUser(jellyfin_user_id="u1", display_name="Rob")
        self.db.add(self.user)
        self.db.commit()
        self.lst = ListSubscription(user_id=self.user.id, name="Top Lithuania",
                                    type="imdb_rss", tag=self.TAG,
                                    url="https://www.imdb.com/list/ls055592025/")
        self.db.add(self.lst)
        self.db.commit()
        # What the last good refresh stored: two movies and two TV shows,
        # all in the library and tagged.
        for tmdb_id, mtype in ((101, "movie"), (102, "movie"), (201, "series"), (202, "series")):
            self.db.add(ListItem(list_id=self.lst.id, tmdb_id=tmdb_id,
                                 imdb_id=f"tt0000{tmdb_id}", media_type=mtype,
                                 title=f"T{tmdb_id}", year="2020"))
            nfo = self.tmp / f"{tmdb_id}.nfo"
            root = "movie" if mtype == "movie" else "tvshow"
            nfo.write_text(f"<{root}><title>T{tmdb_id}</title><tag>{self.TAG}</tag></{root}>")
            model = Movie if mtype == "movie" else Series
            self.db.add(model(tmdb_id=tmdb_id, title=f"T{tmdb_id}", source="provider_1",
                              tags=[self.TAG], nfo_path=str(nfo)))
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def _run_fetch(self, post, get=None):
        from routers import lists
        with mock.patch.object(lists.requests, "post", side_effect=post), \
             mock.patch.object(lists.requests, "get",
                               side_effect=get or (lambda *a, **k: _Resp(200, SERVARR_MOVIES))), \
             mock.patch.object(lists, "_get_tmdb_service", return_value=_FakeTMDB()), \
             mock.patch("services.smartlists._notify_jellyfin_plugin"):
            return lists.fetch_list(self.lst.id, db=self.db, user=self.user)

    def _stored(self):
        from models.database import ListItem
        return {(r.tmdb_id, r.media_type) for r in
                self.db.query(ListItem).filter(ListItem.list_id == self.lst.id)}

    def _tagged(self, model, tmdb_id):
        row = self.db.query(model).filter(model.tmdb_id == tmdb_id).one()
        nfo_has = self.TAG in Path(row.nfo_path).read_text()
        return self.TAG in (row.tags or []) and nfo_has


class TestGraphQLBlockedFallsBackToMoviesOnly(_ListCase):
    def _forbidden(self, *a, **k):
        return _Resp(403, None)

    def test_tv_shows_already_on_the_list_are_kept(self):
        self._run_fetch(self._forbidden)
        self.assertEqual(self._stored(),
                         {(101, "movie"), (102, "movie"), (201, "series"), (202, "series")})

    def test_tv_shows_keep_the_list_tag(self):
        from models.database import Series
        self._run_fetch(self._forbidden)
        self.assertTrue(self._tagged(Series, 201))
        self.assertTrue(self._tagged(Series, 202))

    def test_the_user_is_told(self):
        result = self._run_fetch(self._forbidden)
        self.db.refresh(self.lst)
        self.assertIn("TV shows unavailable", self.lst.last_fetch_note or "")
        self.assertIn("HTTP 403", self.lst.last_fetch_note)
        self.assertEqual(result["note"], self.lst.last_fetch_note)

    def test_a_movie_that_left_the_list_still_goes(self):
        """The fallback is complete for movies, so movie removals still apply."""
        from models.database import Movie
        only_one = [SERVARR_MOVIES[0]]
        self._run_fetch(self._forbidden, get=lambda *a, **k: _Resp(200, only_one))
        self.assertNotIn((102, "movie"), self._stored())
        self.assertFalse(self._tagged(Movie, 102))
        self.assertIn((201, "series"), self._stored())


class TestALaterPageFails(_ListCase):
    def test_items_after_the_failed_page_are_kept(self):
        from models.database import Series
        pages = iter([
            _Resp(200, _gql_page([("tt0000101", "Movie One", "movie"),
                                  ("tt0000201", "Show One", "tvSeries")], next_cursor="c1")),
            _Resp(403, None),
        ])
        self._run_fetch(lambda *a, **k: next(pages))
        # Page 1 held 101 and 201; 102 and 202 were on the page that failed.
        self.assertEqual(self._stored(),
                         {(101, "movie"), (102, "movie"), (201, "series"), (202, "series")})
        self.assertTrue(self._tagged(Series, 202))
        self.db.refresh(self.lst)
        self.assertIn("Only the first 2 items", self.lst.last_fetch_note or "")

    def test_a_complete_read_still_replaces_the_list(self):
        """No regression: a full GraphQL answer is authoritative, removals and all."""
        from models.database import Series
        page = _Resp(200, _gql_page([("tt0000101", "Movie One", "movie"),
                                     ("tt0000201", "Show One", "tvSeries")]))
        self._run_fetch(lambda *a, **k: page)
        self.assertEqual(self._stored(), {(101, "movie"), (201, "series")})
        self.assertFalse(self._tagged(Series, 202))
        self.db.refresh(self.lst)
        self.assertIsNone(self.lst.last_fetch_note)


class TestRefreshAllAndCreate(_ListCase):
    def test_refresh_all_reports_the_partial_read(self):
        from routers import lists
        with mock.patch.object(lists.requests, "post", return_value=_Resp(403, None)), \
             mock.patch.object(lists.requests, "get", return_value=_Resp(200, SERVARR_MOVIES)), \
             mock.patch.object(lists, "_get_tmdb_service", return_value=_FakeTMDB()):
            result = lists.refresh_all_lists(db=self.db, user=self.user)
        self.assertEqual(result["refreshed"], 1)
        self.assertEqual(len(result["warnings"]), 1)
        self.assertIn((201, "series"), self._stored())

    def test_an_imdb_list_with_name_and_url_swapped_is_refused(self):
        from fastapi import HTTPException
        from routers import lists
        body = lists.ListCreate(name="https://www.imdb.com/list/ls055592025/",
                                type="imdb_rss", url="Top Lithuania", tag="x")
        with self.assertRaises(HTTPException) as caught:
            lists.create_list(body, db=self.db, user=self.user)
        self.assertEqual(caught.exception.status_code, 400)

    def test_every_supported_imdb_url_form_is_accepted(self):
        from routers import lists
        for url in ("https://www.imdb.com/list/ls055592025/",
                    "https://www.imdb.com/chart/toptv/",
                    "https://www.imdb.com/user/ur62440355/watchlist"):
            with self.subTest(url=url):
                body = lists.ListCreate(name="n", type="imdb_rss", url=url, tag="t")
                self.assertTrue(lists.create_list(body, db=self.db, user=self.user)["success"])


if __name__ == "__main__":
    unittest.main()
