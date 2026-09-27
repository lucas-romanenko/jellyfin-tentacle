"""#185: two different films with the same title and year.

The movie sync's title map (`known_titles`) held one TMDB id per (title, year),
so a second film with the same name was counted as an "existing" copy of the
first and never imported. Name matching cannot tell the two apart anyway (the
same name gives the same search result); the provider's own TMDB id on each
stream ("tmdb", sent by XUI-style panels) can. These tests pin that:
both import, into separate folders, and nothing already imported moves,
is rewritten or is re-matched. Self-contained: no network.
"""
import shutil
import tempfile
import unittest
from pathlib import Path as _RealPath

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb
from models.database import Movie, Provider, ProviderCategory
import services.sync as sync


def _meta(tid, title, year):
    return {"tmdb_id": tid, "title": title, "year": year, "overview": "", "genres": [],
            "poster_path": None, "backdrop_path": None, "rating": None, "runtime": None}


class TMDB:
    """search: clean provider name -> tmdb id; details: tmdb id -> (title, year)."""
    search = {}
    films = {}
    calls = []
    enabled = True

    def __init__(self, *a, **k):
        pass

    def search_movie(self, name, year=None, **k):
        TMDB.calls.append(("search", name))
        tid = TMDB.search.get(name)
        return _meta(tid, *TMDB.films[tid]) if tid else None

    def get_movie_details(self, tid, **k):
        TMDB.calls.append(("details", tid))
        return _meta(tid, *TMDB.films[tid]) if tid in TMDB.films else None

    def search_series(self, *a, **k):
        return None

    def get_series_details(self, *a, **k):
        return None

    def cleanup_cache(self):
        pass


class Client:
    def __init__(self):
        self.movies = {}  # category -> [stream dict]

    def get_vod_streams(self, cat):
        return [dict(s) for s in self.movies.get(cat, [])]

    def movie_stream_url(self, sid, ext):
        return f"http://provider/movie/u/p/{sid}.{ext}"

    def get_series_list(self, cat):
        return []

    def get_series_info(self, sid):
        return {"episodes": {}}

    def episode_stream_url(self, e, ext):
        return f"http://provider/series/u/p/{e}.{ext}"


def stream(name, sid, hint=None):
    s = {"name": name, "stream_id": sid, "container_extension": "mp4"}
    if hint is not None:
        s["tmdb"] = str(hint)
    return s


class Base(unittest.TestCase):
    require_tmdb = True

    def setUp(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        engine = create_engine(f"sqlite:///{tmp}/t.db")
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.vod = _RealPath(tmp) / "vod"
        (self.vod / "movies").mkdir(parents=True)
        (self.vod / "shows").mkdir(parents=True)
        vod = self.vod

        def mapped(*parts):
            s = str(_RealPath(*parts))
            return _RealPath(str(vod) + s[len("/media/vod"):]) if s.startswith("/media/vod") else _RealPath(*parts)

        self._saved = {k: getattr(sync, k) for k in
                       ("Path", "TMDBService", "make_provider_client", "VOD_MOVIES_ROOT", "VOD_SERIES_ROOT")}
        sync.Path = mapped
        sync.TMDBService = TMDB
        sync.VOD_MOVIES_ROOT = vod / "movies"
        sync.VOD_SERIES_ROOT = vod / "shows"
        self.client = Client()
        sync.make_provider_client = lambda p: self.client
        TMDB.search, TMDB.films, TMDB.calls = {}, {}, []
        self.p = Provider(name="P", server_url="http://provider", username="u", password="p", active=True,
                          require_tmdb_match=self.require_tmdb)
        self.db.add(self.p)
        self.db.commit()
        for c in ("a", "b"):
            self.db.add(ProviderCategory(provider_id=self.p.id, category_id=c, category_name=c, type="movie",
                                         whitelisted=True, source_tag=f"T{c}"))
        self.db.commit()

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(sync, k, v)
        self.db.close()
        self.db.bind.dispose()

    def night(self):
        TMDB.calls = []
        run = sync.sync_provider(self.p, "full", self.db)
        self.assertEqual(run.status, "completed", run.error_message)
        sync.sweep_orphaned_vod_records(self.db)
        self.db.expire_all()
        return run

    def tree(self):
        out = {}
        for f in sorted(self.vod.rglob("*")):
            out[str(f.relative_to(self.vod))] = f.read_bytes() if f.is_file() else b"<dir>"
        return out

    def row(self, tid):
        return self.db.query(Movie).filter_by(tmdb_id=tid).one()

    def plays(self, tid):
        return _RealPath(self.row(tid).strm_path).read_text()


# Two real TMDB films called "Brothers", both 2024; name search finds 1001.
BROTHERS = {1001: ("Brothers", "2024"), 2002: ("Brothers", "2024")}


class NamesakesBothImport(Base):
    def setUp(self):
        super().setUp()
        TMDB.films = dict(BROTHERS)
        TMDB.search = {"Brothers": 1001}

    def test_both_films_import_on_the_first_sync(self):
        self.client.movies = {"a": [stream("Brothers (2024)", 11, 1001), stream("Brothers (2024)", 22, 2002)]}
        self.night()
        self.assertEqual(sorted(m.tmdb_id for m in self.db.query(Movie)), [1001, 2002])
        self.assertNotEqual(self.row(1001).strm_path, self.row(2002).strm_path)
        self.assertIn("/11.mp4", self.plays(1001))
        self.assertIn("/22.mp4", self.plays(2002))
        self.assertIn("<tmdbid>1001</tmdbid>", _RealPath(self.row(1001).nfo_path).read_text())
        self.assertIn("<tmdbid>2002</tmdbid>", _RealPath(self.row(2002).nfo_path).read_text())

    def test_order_does_not_matter(self):
        self.client.movies = {"a": [stream("Brothers (2024)", 22, 2002), stream("Brothers (2024)", 11, 1001)]}
        self.night()
        self.assertEqual(sorted(m.tmdb_id for m in self.db.query(Movie)), [1001, 2002])
        self.assertIn("/11.mp4", self.plays(1001))
        self.assertIn("/22.mp4", self.plays(2002))

    def test_across_categories(self):
        self.client.movies = {"a": [stream("Brothers (2024)", 11, 1001)], "b": [stream("Brothers (2024)", 22, 2002)]}
        self.night()
        self.assertEqual(sorted(m.tmdb_id for m in self.db.query(Movie)), [1001, 2002])

    def test_second_sync_changes_nothing_and_looks_nothing_up(self):
        self.client.movies = {"a": [stream("Brothers (2024)", 11, 1001)], "b": [stream("Brothers (2024)", 22, 2002)]}
        self.night()
        before = self.tree()
        run = self.night()
        self.assertEqual(self.tree(), before)
        self.assertEqual(TMDB.calls, [], "a known namesake was looked up again")
        self.assertEqual((run.movies_new, run.movies_existing), (0, 2))

    def test_upgrade_adds_only_the_missing_film(self):
        """A library from before the fix (the provider's ids were never read)
        keeps every file byte for byte; only the shadowed film is added."""
        self.client.movies = {"a": [stream("Brothers (2024)", 11)], "b": [stream("Brothers (2024)", 22)]}
        self.night()
        self.assertEqual([m.tmdb_id for m in self.db.query(Movie)], [1001])  # the old outcome
        before = self.tree()
        old_path = self.row(1001).strm_path
        self.client.movies = {"a": [stream("Brothers (2024)", 11, 1001)], "b": [stream("Brothers (2024)", 22, 2002)]}
        self.night()
        after = self.tree()
        self.assertEqual({k: v for k, v in after.items() if k in before}, before, "an existing file changed")
        self.assertEqual(set(before) - set(after), set(), "an existing path went away")
        added = sorted(set(after) - set(before))
        self.assertEqual(added, ["movies/Brothers (2024) [tmdbid-2002]",
                                 "movies/Brothers (2024) [tmdbid-2002]/Brothers (2024) [tmdbid-2002].nfo",
                                 "movies/Brothers (2024) [tmdbid-2002]/Brothers (2024) [tmdbid-2002].strm"])
        self.assertEqual(self.row(1001).strm_path, old_path)
        self.assertIn("/22.mp4", self.plays(2002))
        self.night()
        self.assertEqual(self.tree(), after, "not stable on the next night")

    def test_dropping_one_namesake_keeps_the_other(self):
        self.client.movies = {"a": [stream("Brothers (2024)", 11, 1001)], "b": [stream("Brothers (2024)", 22, 2002),
                                                                              stream("Keep (2019)", 33)]}
        TMDB.films[4000] = ("Keep", "2019")
        TMDB.search["Keep"] = 4000
        self.night()
        self.client.movies["b"] = [stream("Keep (2019)", 33)]
        self.night()
        self.night()
        self.assertIsNone(self.db.query(Movie).filter_by(tmdb_id=2002).first())
        self.assertTrue(_RealPath(self.row(1001).strm_path).exists())
        self.assertIn("/11.mp4", self.plays(1001))


class HintsThatMustNotChangeAnything(Base):
    def setUp(self):
        super().setUp()
        TMDB.films = dict(BROTHERS)
        TMDB.films[3003] = ("Something Else", "1999")
        TMDB.search = {"Brothers": 1001}

    def test_a_hint_for_another_title_is_ignored(self):
        self.client.movies = {"a": [stream("Brothers (2024)", 11, 1001)], "b": [stream("Brothers (2024)", 22, 3003)]}
        self.night()
        self.assertEqual([m.tmdb_id for m in self.db.query(Movie)], [1001])
        self.assertIn("/11.mp4", self.plays(1001))

    def test_same_film_listed_twice_stays_one_film(self):
        self.client.movies = {"a": [stream("Brothers (2024)", 11, 1001)],
                              "b": [stream("Brothers (2024)", 22, 1001), stream("Brothers (2024)", 23)]}
        self.night()
        self.assertEqual([m.tmdb_id for m in self.db.query(Movie)], [1001])
        self.night()
        self.assertEqual(TMDB.calls, [], "a second listing of a known film was looked up")

    def test_a_stream_keeps_the_film_it_already_plays(self):
        """A row created from a stream whose hint names the namesake is not
        re-matched: the stream stays on the film its .strm already plays."""
        self.client.movies = {"a": [stream("Brothers (2024)", 11)]}
        self.night()
        before = self.tree()
        self.client.movies = {"a": [stream("Brothers (2024)", 11, 2002)]}
        self.night()
        self.assertEqual([m.tmdb_id for m in self.db.query(Movie)], [1001])
        self.assertEqual(self.tree(), before)


class ProviderOnlyTitles(Base):
    """require_tmdb off and no TMDB match: synthetic ids. A hint must not split
    one provider-only title into one row per listing."""
    require_tmdb = False

    def test_listings_of_a_provider_only_title_stay_one_row(self):
        TMDB.search = {}
        self.client.movies = {"a": [stream("Home Video (2024)", 11, 555)],
                              "b": [stream("Home Video (2024)", 22, 556)]}
        self.night()
        before = self.tree()
        self.night()
        self.assertEqual(self.db.query(Movie).count(), 1)
        self.assertEqual(self.tree(), before)


class FolderHelpers(unittest.TestCase):
    def test_hint_parsing(self):
        h = sync._provider_tmdb_hint
        self.assertEqual(h({"tmdb": "2002"}), 2002)
        self.assertEqual(h({"tmdb_id": 7}), 7)
        for bad in ("", "0", "tt123", None, "-5", "99999999999", "²", "2002²", "٣", "１２", True, [2002]):
            self.assertIsNone(h({"tmdb": bad}), bad)
        self.assertEqual(h({"tmdb": " 2002\n"}), 2002)


class OddHintsNeverFailTheSync(Base):
    def test_superscript_and_non_ascii_digits(self):
        """"²".isdigit() is True but int("²") raises: the whole provider
        sync failed, every night."""
        TMDB.films = dict(BROTHERS)
        TMDB.search = {"Brothers": 1001}
        self.client.movies = {"a": [stream("Brothers (2024)", 11, "²"), stream("Brothers (2024)", 22, "٣")]}
        self.night()
        self.assertEqual([m.tmdb_id for m in self.db.query(Movie)], [1001])


if __name__ == "__main__":
    unittest.main()


class MergedRadarrFolder(Base):
    """The merged layout: Radarr's folder and Tentacle's VOD folder are one
    folder on disk. Radarr writes no NFO by default, so only its row (by folder
    name) and the video file say the folder is taken."""

    def setUp(self):
        super().setUp()
        TMDB.films = dict(BROTHERS)
        TMDB.search = {"Brothers": 1001}

    def _radarr(self, tid=1001, row=True):
        folder = self.vod / "movies" / "Brothers (2024)"
        folder.mkdir(parents=True)
        (folder / "Brothers (2024) Bluray-1080p.mkv").write_bytes(b"x" * 10)
        if row:
            self.db.add(Movie(tmdb_id=tid, title="Brothers", year="2024", source="radarr",
                              radarr_path="/data/movies/Brothers (2024)/Brothers (2024) Bluray-1080p.mkv"))
            self.db.commit()
        return folder

    def test_a_namesake_is_not_written_into_radarrs_folder(self):
        folder = self._radarr()
        self.client.movies = {"a": [stream("Brothers (2024)", 22, 2002)]}
        self.night()
        self.assertEqual(sorted(p.name for p in folder.iterdir()), ["Brothers (2024) Bluray-1080p.mkv"])
        self.assertIn("[tmdbid-2002]", self.row(2002).strm_path)

    def test_same_film_as_radarr_stays_a_duplicate(self):
        folder = self._radarr()
        self.client.movies = {"a": [stream("Brothers (2024)", 11, 1001)]}
        self.night()
        self.assertEqual(sorted(p.name for p in folder.iterdir()), ["Brothers (2024) Bluray-1080p.mkv"])
        self.assertEqual([(m.tmdb_id, m.source) for m in self.db.query(Movie)], [(1001, "radarr")])
        self.assertEqual(sorted(p.name for p in (self.vod / "movies").iterdir()), ["Brothers (2024)"])

    def test_a_video_with_no_nfo_and_no_row_is_an_unknown_owner_for_a_namesake(self):
        """Only a namesake import treats an unmanaged video as another film's
        (CONTRACT-185 D4); a plain import writes next to it (R2,
        ContractUnmanagedVideo)."""
        folder = self._radarr(row=False)
        self.client.movies = {"a": [stream("Brothers (2024)", 22, 2002)]}
        self.night()
        self.assertEqual(sorted(p.name for p in folder.iterdir()), ["Brothers (2024) Bluray-1080p.mkv"])
        self.assertIn("[tmdbid-2002]", self.row(2002).strm_path)


class WrongIdsCostNothingExtra(Base):
    """A provider id that names an unrelated film, on a second listing of a
    known film, must not cost a name search every night; the hinted film's
    details are looked up at most once per sync."""

    def test_no_name_search_for_a_wrong_id_on_a_known_title(self):
        TMDB.films = dict(BROTHERS)
        TMDB.films[3003] = ("Other", "1999")
        TMDB.search = {"Brothers": 1001}
        self.client.movies = {"a": [stream("Brothers (2024)", 11, 1001)],
                              "b": [stream("Brothers (2024)", 12, 3003), stream("Brothers (2024)", 13, 3003),
                                    stream("Brothers (2024)", 14, 999999)]}
        self.night()
        self.night()
        self.assertEqual([m.tmdb_id for m in self.db.query(Movie)], [1001])
        self.assertNotIn(("search", "Brothers"), TMDB.calls, TMDB.calls)
        self.assertEqual(sorted(TMDB.calls), [("details", 3003), ("details", 999999)])


class DetailsNotFoundIsCached(unittest.TestCase):
    def setUp(self):
        import tempfile, shutil
        from services.tmdb import TMDBService
        d = tempfile.mkdtemp(); self.addCleanup(shutil.rmtree, d, True)
        self.svc = TMDBService("token", d)
        self.calls = []

    def _answer(self, status):
        import requests
        test = self

        class Resp:
            status_code = status
            def raise_for_status(self):
                if status >= 400:
                    raise requests.HTTPError(response=self)
            def json(self):
                return {"id": 5, "title": "X", "release_date": "2001-01-01"}

        def get(url, params=None, timeout=None):
            test.calls.append(url)
            return Resp()
        self.svc.session.get = get

    def test_a_404_is_remembered(self):
        self._answer(404)
        self.assertIsNone(self.svc.get_movie_details(80000001))
        self.assertIsNone(self.svc.get_movie_details(80000001))
        self.assertEqual(len(self.calls), 1, "an id TMDB does not know was requested again")

    def test_a_rate_limit_is_not_remembered(self):
        self._answer(429)
        self.assertIsNone(self.svc.get_movie_details(5))
        self._answer(200)
        self.assertEqual(self.svc.get_movie_details(5)["tmdb_id"], 5)


class SwappedPairIsLoggedOnly(Base):
    """A library from before the fix whose row was created by the NAMESAKE's
    stream: row 1001 plays stream 22 (film 2002). A panel that swaps two
    namesakes' ids shows the same signals, so nothing is rewritten (CONTRACT-185
    D6, R1a): the pair is logged once per row per sync, and film 2002 stays
    missing until an admin uses Wrong movie."""

    def setUp(self):
        super().setUp()
        TMDB.films = dict(BROTHERS)
        TMDB.search = {"Brothers": 1001}

    def _old_library(self):
        self.client.movies = {"a": [stream("Brothers (2024)", 22)]}
        self.night()
        self.assertIn("/22.mp4", self.plays(1001))
        return self.tree(), self.row(1001).strm_path

    def _check_logged_only(self, before, logs):
        after = self.tree()
        self.assertEqual(set(after), set(before))  # no path added, moved or removed
        self.assertEqual({k: v for k, v in after.items() if k.endswith(".strm")},
                         {k: v for k, v in before.items() if k.endswith(".strm")})  # no file rewritten
        self.assertEqual([m.tmdb_id for m in self.db.query(Movie)], [1001])
        lines = [l for l in logs.output if "Suspected swapped namesake ids" in l]
        self.assertEqual(len(lines), 1, logs.output)
        self.assertIn("row TMDB 1001 plays stream 22 (provider id 2002); stream 11 has provider id 1001. "
                      "Not changed: fix it with Wrong movie.", lines[0])

    def test_logged_when_the_wrong_stream_comes_first(self):
        before, _ = self._old_library()
        self.client.movies = {"a": [stream("Brothers (2024)", 22, 2002), stream("Brothers (2024)", 11, 1001)]}
        with self.assertLogs("services.sync", "INFO") as logs:
            self.night()
        self._check_logged_only(before, logs)

    def test_logged_when_the_right_stream_comes_first(self):
        before, _ = self._old_library()
        self.client.movies = {"a": [stream("Brothers (2024)", 11, 1001)], "b": [stream("Brothers (2024)", 22, 2002)]}
        with self.assertLogs("services.sync", "INFO") as logs:
            self.night()
        self._check_logged_only(before, logs)

    def test_nothing_when_tmdb_disagrees(self):
        before, _ = self._old_library()
        TMDB.films[2002] = ("Brothers", "2023")  # TMDB says the other film is not a namesake
        self.client.movies = {"a": [stream("Brothers (2024)", 22, 2002), stream("Brothers (2024)", 11, 1001)]}
        self.night()
        self.assertEqual(self.tree(), before)
        self.assertEqual([m.tmdb_id for m in self.db.query(Movie)], [1001])

    def test_nothing_when_the_nfo_names_another_film(self):
        before, _ = self._old_library()
        nfo = _RealPath(self.row(1001).nfo_path)
        nfo.write_text(nfo.read_text().replace("<tmdbid>1001</tmdbid>", "<tmdbid>2002</tmdbid>"))
        before = self.tree()
        self.client.movies = {"a": [stream("Brothers (2024)", 22, 2002), stream("Brothers (2024)", 11, 1001)]}
        self.night()
        self.assertIn("/22.mp4", self.plays(1001))
        self.assertEqual({k: v for k, v in self.tree().items() if k in before}, before)

    def test_nothing_without_the_right_streams_id(self):
        before, _ = self._old_library()
        self.client.movies = {"a": [stream("Brothers (2024)", 22, 2002), stream("Brothers (2024)", 11)]}
        self.night()
        self.assertIn("/22.mp4", self.plays(1001))
        self.assertEqual({k: v for k, v in self.tree().items() if k in before}, before)


class NiceToHaves(Base):
    def test_same_stream_number_of_another_account_is_not_the_rows_stream(self):
        """CONTRACT-185 D3: another stream only on positive evidence (another
        configured provider's host AND username, or its VOD token); an older
        host of this provider is still this provider (a host change)."""
        other = Provider(name="Other", server_url="http://provider", username="u2", password="p",
                         active=True, priority=2)
        self.db.add(other)
        self.db.commit()
        index = sync._MovieIndex(self.db, self.p)
        f = _RealPath(self.vod) / "x.strm"
        s = stream("Brothers (2024)", 11)
        f.write_text("http://provider/movie/u2/p/11.mp4")  # the other account on the same panel
        self.assertIs(sync._movie_row_plays_stream(self.client, s, str(f), index), False)
        f.write_text("http://old-host/movie/u/p/11.mp4")  # this provider before a host change
        self.assertIs(sync._movie_row_plays_stream(self.client, s, str(f), index), True)
        f.write_text("http://provider/movie/u/p/11.mp4")
        self.assertIs(sync._movie_row_plays_stream(self.client, s, str(f), index), True)
        f.write_text("http://provider/movie/u/p/12.mp4")
        self.assertIs(sync._movie_row_plays_stream(self.client, s, str(f), index), False)


class SharedFileIsLeftAlone(SwappedPairIsLoggedOnly):
    """Two rows that already share one .strm (a #155 collision from before):
    nothing is rewritten, and no swap is reported for it."""

    def test_logged_when_the_wrong_stream_comes_first(self):
        before, path = self._old_library()
        self.db.add(Movie(tmdb_id=3003, title="Brothers", year="2024", source="provider_1",
                          provider_id=self.p.id, strm_path=path, nfo_path=path[:-5] + ".nfo"))
        self.db.commit()
        self.client.movies = {"a": [stream("Brothers (2024)", 22, 2002), stream("Brothers (2024)", 11, 1001)]}
        self.night()
        self.assertIn("/22.mp4", self.plays(1001))
        self.assertIn("/22.mp4", self.plays(3003))

    test_logged_when_the_right_stream_comes_first = None
    test_nothing_when_tmdb_disagrees = None
    test_nothing_when_the_nfo_names_another_film = None
    test_nothing_without_the_right_streams_id = None


class RenumberedStreamKeepsItsRow(Base):
    """The provider renumbers its streams and now sends an id naming a
    namesake. The row's own stream is gone, so nothing shows the known film
    is held by another stream: the stream stays the known film (as before)
    rather than importing the namesake and letting the old row be pruned
    (a lost Jellyfin item). Found by the property test (seed 42)."""

    def test_row_kept_and_nothing_added(self):
        TMDB.films = dict(BROTHERS)
        TMDB.search = {"Brothers": 1001}
        self.client.movies = {"a": [stream("Brothers (2024)", 11)]}
        self.night()
        before = self.tree()
        self.client.movies = {"a": [stream("Brothers (2024)", 111, 2002)]}
        for _ in range(3):
            self.night()
        self.assertEqual([m.tmdb_id for m in self.db.query(Movie)], [1001])
        self.assertEqual(self.tree(), before)


# ── CONTRACT-185.md: one test per event row the review found (each failed on 0653661) ──

class ContractSwappedIds(Base):
    """E6 / R1a: a panel that swaps two namesakes' ids must never make a right
    row wrong (review H1). Row 1001 correctly plays stream 11."""

    def test_a_right_row_is_not_crossed(self):
        TMDB.films = dict(BROTHERS)
        TMDB.search = {"Brothers": 1001}
        self.client.movies = {"a": [stream("Brothers (2024)", 11)]}
        self.night()
        self.assertIn("/11.mp4", self.plays(1001))
        self.client.movies = {"a": [stream("Brothers (2024)", 11, 2002), stream("Brothers (2024)", 22, 1001)]}
        self.night()
        self.night()
        self.assertIn("/11.mp4", self.plays(1001), "the right row was rewritten to the other film's stream")


class ContractHostChange(Base):
    """E14 / S5: a provider host change is the same provider. Row 1001 plays
    stream 11, listed twice; after the host change stream 11 carries id 2002.
    Stream 11 is still 1001's own stream (by number, at the old host): no second
    row may play it."""

    def test_no_second_row_for_the_same_stream(self):
        TMDB.films = dict(BROTHERS)
        TMDB.search = {"Brothers": 1001}
        self.client.movies = {"a": [stream("Brothers (2024)", 11)], "b": [stream("Brothers (2024)", 33)]}
        self.night()
        self.assertIn("/11.mp4", self.plays(1001))
        self.p.server_url = "http://provider2"
        self.db.commit()
        self.client.movie_stream_url = lambda sid, ext: f"http://provider2/movie/u/p/{sid}.{ext}"
        self.client.movies = {"a": [stream("Brothers (2024)", 11, 2002)], "b": [stream("Brothers (2024)", 33)]}
        self.night()
        self.night()
        playing_11 = [m.tmdb_id for m in self.db.query(Movie) if self.plays(m.tmdb_id).endswith("/11.mp4")]
        self.assertEqual(playing_11, [1001])


class ContractUnmanagedVideo(Base):
    """E17 / R2: a plain import next to a video nobody manages (no Radarr row,
    no NFO) behaves like 755ea67: the plain folder (review test_U)."""

    def test_plain_import_keeps_the_plain_folder(self):
        TMDB.films = {4000: ("Keep", "2019")}
        TMDB.search = {"Keep": 4000}
        folder = self.vod / "movies" / "Keep (2019)"
        folder.mkdir(parents=True)
        (folder / "home video.mkv").write_bytes(b"v")
        self.client.movies = {"a": [stream("Keep (2019)", 11)]}
        self.night()
        self.assertEqual(_RealPath(self.row(4000).strm_path).parent.name, "Keep (2019)")


class ContractClaimNeverTakesOver(Base):
    """E26 / O1: a higher-priority provider's stream whose id names a namesake
    another provider owns records a duplicate only: the row keeps its owner."""

    def test_owner_unchanged(self):
        TMDB.films = dict(BROTHERS)
        TMDB.search = {"Brothers": 1001}
        low = self.p
        low.priority = 2
        high = Provider(name="High", server_url="http://high", username="u", password="p", active=True,
                        priority=1, require_tmdb_match=True)
        self.db.add(high)
        self.db.commit()
        self.db.add(ProviderCategory(provider_id=high.id, category_id="h", category_name="h", type="movie",
                                     whitelisted=True, source_tag="Th"))
        self.db.commit()
        high_client = Client()
        high_client.movie_stream_url = lambda sid, ext: f"http://high/movie/u/p/{sid}.{ext}"
        sync.make_provider_client = lambda p: high_client if p.id == high.id else self.client
        self.client.movies = {"a": [stream("Brothers (2024)", 11, 1001), stream("Brothers (2024)", 22, 2002)]}
        TMDB.calls = []
        sync.sync_provider(low, "full", self.db)
        self.db.expire_all()
        self.assertEqual(self.row(2002).provider_id, low.id)
        high_client.movies = {"h": [stream("Brothers (2024)", 99, 2002)]}  # really film 1001, mislabelled
        for _ in range(2):
            sync.sync_provider(high, "full", self.db)
            sync.sync_provider(low, "full", self.db)
            self.db.expire_all()
        self.assertEqual(self.row(2002).provider_id, low.id, "a provider id alone took the row over")


class ContractRestoreOnlyOwnStream(Base):
    """E25: a missing .strm of a namesake row is restored only from a stream
    that is that row's own (its id names the row, or the row is the only one
    with its title). Here TMDB search is failing, so every stream falls back
    to the title map."""

    def test_never_restored_from_the_other_films_stream(self):
        TMDB.films = dict(BROTHERS)
        TMDB.search = {"Brothers": 1001}
        self.client.movies = {"a": [stream("Brothers (2024)", 11, 1001), stream("Brothers (2024)", 22, 2002)]}
        self.night()
        self.assertIn("/22.mp4", self.plays(2002))
        _RealPath(self.row(2002).strm_path).unlink()
        TMDB.search = {}
        self.client.movies = {"a": [stream("Brothers (2024)", 11), stream("Brothers (2024)", 33),
                                    stream("Brothers (2024)", 22)]}  # 33 is film 1001 again
        self.night()
        f = _RealPath(self.row(2002).strm_path)
        if f.exists():
            self.assertNotIn("/33.mp4", f.read_text(), "film 2002's file now plays film 1001")
            self.assertNotIn("/11.mp4", f.read_text(), "film 2002's file now plays film 1001")
