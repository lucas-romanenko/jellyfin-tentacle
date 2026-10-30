"""#871: library folders Radarr/Sonarr don't know about, and adding them with
the folder as it is.

A fake Radarr + Sonarr answer over real HTTP; the library roots
(services.media_files.RADARR_MEDIA_ROOT / SONARR_MEDIA_ROOT) point at a temp
dir; TMDB answers from a table. Checked: which folders are listed (real
video, not in Radarr/Sonarr by folder name; .strm-only and empty ones not),
the match and how sure it is (NFO id, id in the folder name, title + year,
in doubt), and that Add sends the folder as the path with the default
quality profile and no search, and that no file in the library changes.
"""
import json
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlparse

from web_stubs import _ensure_web_stubs

_ensure_web_stubs()

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

import models.database as mdb  # noqa: E402
from tmp_dirs import temp_dir  # noqa: E402


class _FakeArr(BaseHTTPRequestHandler):
    routes = {}
    posts = []

    def log_message(self, *a):
        pass

    def _answer(self, status, body):
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/api/v3/series/lookup":
            # Skyhook: the show the term names (tvdb:N)
            term = parse_qs(u.query).get("term", [""])[0]
            n = int(term.split(":")[1]) if term.startswith("tvdb:") else 0
            names = {81189: ("Breaking Bad", 1396), 121361: ("Game of Thrones", 1399)}
            body = [{"title": names[n][0], "tvdbId": n, "tmdbId": names[n][1], "titleSlug": "x",
                     "seasons": [{"seasonNumber": 1, "monitored": True}], "images": []}] if n in names else []
            return self._answer(200, body)
        status, body = _FakeArr.routes.get(("GET", u.path), (200, []))
        self._answer(status, body)

    def do_POST(self):
        path = urlparse(self.path).path
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        payload = json.loads(raw or b"{}")
        _FakeArr.posts.append((path, payload))
        self._answer(201, dict(payload, id=99))


# TMDB, by endpoint (+ query for searches)
_MOVIES = {
    348: ("Alien", "1979-05-25"), 603: ("The Matrix", "1999-03-31"), 949: ("Heat", "1995-12-15"),
    438631: ("Dune", "2021-09-15"), 841: ("Dune", "1984-12-14"), 62: ("2001: A Space Odyssey", "1968-04-02"),
}
_SHOWS = {1396: ("Breaking Bad", "2008-01-20"), 1399: ("Game of Thrones", "2011-04-17")}
_SEARCH = {
    ("search/movie", "alien"): [348],
    ("search/movie", "dune"): [438631, 841],
    ("search/movie", "heat"): [949],
    ("search/tv", "game of thrones"): [1399],
}


def _fake_tmdb(self, endpoint, params=None):
    params = params or {}
    if endpoint.startswith("movie/"):
        t = _MOVIES.get(int(endpoint.split("/")[1]))
        return {"id": int(endpoint.split("/")[1]), "title": t[0], "release_date": t[1]} if t else None
    if endpoint.startswith("tv/") and endpoint.endswith("external_ids"):
        return {"tvdb_id": 81189 if "1396" in endpoint else 121361}
    if endpoint.startswith("tv/"):
        t = _SHOWS.get(int(endpoint.split("/")[1]))
        return {"id": int(endpoint.split("/")[1]), "name": t[0], "first_air_date": t[1]} if t else None
    if endpoint.startswith("find/"):
        if params.get("external_source") == "tvdb_id" and endpoint.endswith("/81189"):
            return {"tv_results": [{"id": 1396}], "movie_results": []}
        return {"tv_results": [], "movie_results": []}
    if endpoint in ("search/movie", "search/tv"):
        ids = _SEARCH.get((endpoint, params.get("query", "").lower()), [])
        table = _MOVIES if endpoint == "search/movie" else _SHOWS
        key = "title" if endpoint == "search/movie" else "name"
        date = "release_date" if endpoint == "search/movie" else "first_air_date"
        return {"results": [{"id": i, key: table[i][0], date: table[i][1], "vote_count": 100} for i in ids]}
    return None


def _write(path: Path, data: bytes = b"x" * 1000):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _tree(root: Path) -> dict:
    return {str(p.relative_to(root)): (p.stat().st_size, p.stat().st_mtime_ns)
            for p in sorted(root.rglob("*"))}


class _Base(unittest.TestCase):
    def setUp(self):
        from models.database import set_setting
        from services import media_files, untracked
        _FakeArr.routes, _FakeArr.posts = {}, []
        self.srv = HTTPServer(("127.0.0.1", 0), _FakeArr)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        url = f"http://127.0.0.1:{self.srv.server_port}"

        self.tmp = Path(temp_dir(self))
        engine = create_engine(f"sqlite:///{self.tmp}/t.db", connect_args={"check_same_thread": False})
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)
        for k, v in (("radarr_url", url), ("radarr_api_key", "k"), ("sonarr_url", url), ("sonarr_api_key", "k"),
                     ("data_dir", str(self.tmp)), ("radarr_quality_profile_id", "7"),
                     ("sonarr_quality_profile_id", "8")):
            set_setting(self.db, k, v)

        self.movies, self.shows = self.tmp / "movies", self.tmp / "shows"
        self.movies.mkdir()
        self.shows.mkdir()
        for attr, val in (("RADARR_MEDIA_ROOT", self.movies), ("SONARR_MEDIA_ROOT", self.shows)):
            p = mock.patch.object(media_files, attr, val)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch("services.tmdb.TMDBService._request", _fake_tmdb)
        p.start()
        self.addCleanup(p.stop)
        untracked._state.update(running=False, scanned_at=None, items=[], problems=[], progress=None)

        # Library: Heat is Radarr's; Alien, The Matrix, 2001 and Dune aren't;
        # "VOD Film" is Tentacle's own .strm, "Empty" has nothing to play.
        _write(self.movies / "Heat (1995)" / "Heat (1995).mkv")
        _write(self.movies / "Alien (1979)" / "Alien.1979.1080p.mkv", b"a" * 5000)
        _write(self.movies / "Alien (1979)" / "Alien.1979.1080p.en.srt", b"subs")
        _write(self.movies / "The Matrix (1999) {tmdb-603}" / "matrix.mp4")
        _write(self.movies / "Space film" / "film.mkv")
        _write(self.movies / "Space film" / "movie.nfo",
               b"<?xml version='1.0'?><movie><title>2001</title><uniqueid type='tmdb'>62</uniqueid></movie>")
        _write(self.movies / "Dune" / "Dune.mkv")
        _write(self.movies / "VOD Film (2020)" / "VOD Film (2020).strm", b"http://x")
        (self.movies / "Empty").mkdir()
        # Shows: Breaking Bad has a tvshow.nfo with its TVDB id; GoT none;
        # The Wire is Sonarr's.
        _write(self.shows / "Breaking Bad" / "Season 01" / "S01E01.mkv")
        _write(self.shows / "Breaking Bad" / "tvshow.nfo",
               b"<tvshow><title>Breaking Bad</title><uniqueid type='tvdb'>81189</uniqueid></tvshow>")
        _write(self.shows / "Game of Thrones (2011)" / "Season 1" / "GoT S01E01.mkv")
        _write(self.shows / "The Wire" / "Season 1" / "e1.mkv")

        _FakeArr.routes[("GET", "/api/v3/movie")] = (200, [
            {"id": 1, "tmdbId": 949, "title": "Heat", "path": "/data/movies/Heat (1995)"}])
        _FakeArr.routes[("GET", "/api/v3/series")] = (200, [
            {"id": 2, "tvdbId": 79126, "tmdbId": 1438, "title": "The Wire", "path": "/tv/The Wire"}])
        _FakeArr.routes[("GET", "/api/v3/rootfolder")] = (200, [{"id": 1, "path": "/data/movies"}])
        _FakeArr.routes[("GET", "/api/v3/qualityprofile")] = (200, [{"id": 7, "name": "HD"}, {"id": 8, "name": "TV"}])

    def scan(self):
        from services import untracked
        return untracked.scan(self.db)

    def item(self, st, folder):
        return next((i for i in st["items"] if i["folder"] == folder), None)


class TestTheList(_Base):
    def test_lists_only_real_folders_radarr_sonarr_dont_have(self):
        st = self.scan()
        self.assertEqual(sorted(i["folder"] for i in st["items"]),
                         ["Alien (1979)", "Breaking Bad", "Dune", "Game of Thrones (2011)", "Space film",
                          "The Matrix (1999) {tmdb-603}"], st["problems"])
        self.assertEqual(st["count"], 6)
        self.assertEqual(st["problems"], [])

    def test_match_and_how_sure(self):
        st = self.scan()
        alien = self.item(st, "Alien (1979)")
        self.assertEqual(alien["match"]["tmdb_id"], 348)
        self.assertGreaterEqual(alien["confidence"], 95)
        self.assertFalse(alien["ambiguous"])
        self.assertEqual(alien["size"], 5000)              # its videos (not the subtitles)
        self.assertEqual(alien["arr_path"], "/data/movies/Alien (1979)")

        matrix = self.item(st, "The Matrix (1999) {tmdb-603}")
        self.assertEqual((matrix["match"]["tmdb_id"], matrix["confidence"]), (603, 100))
        self.assertIn("folder name", matrix["how"])

        space = self.item(st, "Space film")
        self.assertEqual((space["match"]["tmdb_id"], space["confidence"]), (62, 100))
        self.assertIn("NFO", space["how"])

        bb = self.item(st, "Breaking Bad")
        self.assertEqual((bb["match"]["tmdb_id"], bb["match"]["tvdb_id"], bb["confidence"]), (1396, 81189, 100))

    def test_two_namesakes_and_no_year_is_a_pick(self):
        dune = self.item(self.scan(), "Dune")
        self.assertTrue(dune["ambiguous"])
        self.assertLess(dune["confidence"], 95)
        self.assertEqual({c["tmdb_id"] for c in dune["candidates"]}, {438631, 841})

    def test_sure_count_leaves_out_the_ones_in_doubt(self):
        st = self.scan()
        self.assertEqual(st["sure"], 5)   # all but Dune

    def test_title_radarr_has_in_another_folder_is_not_addable(self):
        _FakeArr.routes[("GET", "/api/v3/movie")] = (200, [
            {"id": 1, "tmdbId": 949, "path": "/data/movies/Heat (1995)"},
            {"id": 3, "tmdbId": 348, "path": "/data/movies/Alien 4K"}])
        st = self.scan()
        alien = self.item(st, "Alien (1979)")
        self.assertEqual(alien["elsewhere"], "/data/movies/Alien 4K")
        from services import untracked
        outcome, reason = untracked.add(self.db, "movie", "Alien (1979)")
        self.assertEqual(outcome, "failed")
        self.assertIn("another folder", reason)
        self.assertEqual([p for p in _FakeArr.posts if p[0] == "/api/v3/movie"], [])

    def test_radarr_unreadable_says_so_and_lists_nothing_for_it(self):
        _FakeArr.routes[("GET", "/api/v3/movie")] = (500, {"message": "busy"})
        st = self.scan()
        self.assertFalse(any(i["kind"] == "movie" for i in st["items"]))
        self.assertTrue(any("Radarr" in p for p in st["problems"]))

    def test_radarr_root_from_its_titles_not_its_first_root(self):
        # Radarr's first root is a 4K one; its titles Tentacle sees live in /data/movies
        _FakeArr.routes[("GET", "/api/v3/rootfolder")] = (200, [{"path": "/movies4k"}, {"path": "/data/movies"}])
        self.assertEqual(self.item(self.scan(), "Alien (1979)")["arr_path"], "/data/movies/Alien (1979)")

    def test_scan_reads_only(self):
        before = _tree(self.tmp / "movies"), _tree(self.tmp / "shows")
        self.scan()
        self.assertEqual((_tree(self.tmp / "movies"), _tree(self.tmp / "shows")), before)
        self.assertEqual(_FakeArr.posts, [])


class TestAdd(_Base):
    def test_movie_add_uses_the_folder_default_profile_and_no_search(self):
        from services import untracked
        self.scan()
        before = _tree(self.movies)
        outcome, reason = untracked.add(self.db, "movie", "Alien (1979)")
        self.assertEqual(outcome, "added", reason)
        (path, body), = [p for p in _FakeArr.posts if p[0] == "/api/v3/movie"]
        self.assertEqual(body["tmdbId"], 348)
        self.assertEqual(body["path"], "/data/movies/Alien (1979)")
        self.assertEqual(body["qualityProfileId"], 7)
        self.assertFalse(body["addOptions"]["searchForMovie"])
        self.assertFalse(body["monitored"])
        self.assertEqual(_tree(self.movies), before)
        self.assertIsNone(self.item(untracked.state(), "Alien (1979)"))   # off the list

    def test_movie_add_monitored_when_chosen(self):
        from services import untracked
        self.scan()
        untracked.add(self.db, "movie", "Alien (1979)", monitored=True)
        body = [p for p in _FakeArr.posts if p[0] == "/api/v3/movie"][0][1]
        self.assertTrue(body["monitored"])
        self.assertFalse(body["addOptions"]["searchForMovie"])

    def test_pick_sends_the_picked_title(self):
        from services import untracked
        self.scan()
        outcome, _ = untracked.add(self.db, "movie", "Dune", tmdb_id=841)
        self.assertEqual(outcome, "added")
        body = [p for p in _FakeArr.posts if p[0] == "/api/v3/movie"][0][1]
        self.assertEqual((body["tmdbId"], body["path"]), (841, "/data/movies/Dune"))

    def test_series_add_uses_the_folder_and_no_search(self):
        from services import untracked
        self.scan()
        before = _tree(self.shows)
        outcome, reason = untracked.add(self.db, "series", "Breaking Bad")
        self.assertEqual(outcome, "added", reason)
        (path, body), = [p for p in _FakeArr.posts if p[0] == "/api/v3/series"]
        self.assertEqual(body["path"], "/tv/Breaking Bad")
        self.assertNotIn("rootFolderPath", body)
        self.assertEqual(body["qualityProfileId"], 8)
        self.assertFalse(body["addOptions"]["searchForMissingEpisodes"])
        self.assertFalse(body["monitored"])
        self.assertEqual(body["monitorNewItems"], "none")
        self.assertEqual(_tree(self.shows), before)

    def test_no_default_profile_sends_nothing(self):
        from models.database import set_setting
        from services import untracked
        from services.media_requests import RequestRefused
        set_setting(self.db, "radarr_quality_profile_id", "")
        self.scan()
        with self.assertRaises(RequestRefused):
            untracked.add(self.db, "movie", "Alien (1979)")
        self.assertEqual([p for p in _FakeArr.posts if p[0] == "/api/v3/movie"], [])

    def test_only_a_listed_folder_is_sent(self):
        from services import untracked
        self.scan()
        for folder in ("../etc", "Heat (1995)", "Nope"):
            outcome, _ = untracked.add(self.db, "movie", folder, tmdb_id=1)
            self.assertEqual(outcome, "failed", folder)
        self.assertEqual(_FakeArr.posts, [])

    def test_add_sure_skips_the_ones_in_doubt(self):
        from services import untracked
        self.scan()
        r = untracked.add_sure(self.db, monitored=False, via="test")
        self.assertEqual(r["added"], 5, r)
        sent = sorted(b.get("path") for _, b in _FakeArr.posts)
        self.assertNotIn("/data/movies/Dune", sent)
        self.assertEqual([i["folder"] for i in untracked.state()["items"]], ["Dune"])


class TestNightly(_Base):
    def test_off_by_default(self):
        from services import untracked
        self.assertIsNone(untracked.nightly(self.db))
        self.assertEqual(_FakeArr.posts, [])

    def test_on_adds_the_sure_ones(self):
        from models.database import set_setting
        from services import untracked
        set_setting(self.db, "untracked_auto_add", "true")
        r = untracked.nightly(self.db)
        self.assertEqual(r["added"], 5, r)
        self.assertTrue(all(not b["monitored"] for _, b in _FakeArr.posts))


class TestRoutes(_Base):
    def test_routes_are_admin_only(self):
        import routers.library as lib
        from routers.auth import require_admin
        paths = {"/api/library/untracked", "/api/library/untracked/scan", "/api/library/untracked/add",
                 "/api/library/untracked/add-sure", "/api/library/untracked/search",
                 "/api/library/untracked/options"}
        seen = set()
        for route in lib.router.routes:
            if route.path in paths:
                seen.add(route.path)
                self.assertIn(require_admin, [d.dependency for d in route.dependencies], route.path)
        self.assertEqual(seen, paths)

    def test_add_route_answers_with_the_new_count(self):
        import routers.library as lib
        self.scan()
        with mock.patch.object(lib, "_rescan_after_add") as rescan:
            r = lib.add_untracked(lib.UntrackedAddBody(kind="movie", folder="Alien (1979)"), self.db)
        self.assertEqual((r["outcome"], r["count"]), ("added", 5))
        rescan.assert_called_once_with({"movie"})

    def test_options_default_off_and_save(self):
        import routers.library as lib
        self.scan()
        self.assertEqual(lib.list_untracked(self.db)["options"], {"auto_add": False, "monitored": False})
        lib.set_untracked_options(lib.UntrackedOptionsBody(auto_add=True), self.db)
        self.assertEqual(lib.list_untracked(self.db)["options"], {"auto_add": True, "monitored": False})


class TestNames(unittest.TestCase):
    def test_title_and_year(self):
        from services.untracked import ids_in_name, title_and_year
        self.assertEqual(title_and_year("Heat (1995) {tmdb-949}"), ("Heat", "1995"))
        self.assertEqual(title_and_year("Blade.Runner.1982.1080p"), ("Blade Runner", "1982"))
        self.assertEqual(title_and_year("Dune"), ("Dune", None))
        self.assertEqual(title_and_year("1917 (2019)"), ("1917", "2019"))
        self.assertEqual(ids_in_name("Show [tvdbid-81189] {imdb-tt0903747}"), {"imdb": "tt0903747", "tvdb": "81189"})


if __name__ == "__main__":
    unittest.main()
