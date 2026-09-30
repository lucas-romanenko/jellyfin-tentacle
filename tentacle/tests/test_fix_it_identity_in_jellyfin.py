"""#294 follow-up: "Fix it" in place gives the Jellyfin item the right film's
identity, or changes nothing.

d5d19a9 keeps the .strm (so the item and every user's data stay) and asks
Jellyfin for a ReplaceAllMetadata refresh. Two things went wrong live on
Jellyfin 10.11.8:
- with the library's NFO saver on, that refresh skips the NFO: Jellyfin
  kept showing the old film, and Tentacle's tags were wiped;
- when Jellyfin couldn't be reached, "Fixed" was answered anyway and the
  next scan left the old film's parental rating, list tags and cast on the
  item for good (a scan keeps every field the NFO doesn't have).
Now the identity is set with an ItemUpdate (locked fields left alone) before
anything is saved; if Jellyfin doesn't take it, the NFOs are put back and the
admin gets a 502 with nothing changed.

The property test runs SEEDS random cases with a fault at every Jellyfin
call (down / fails / reply lost), the save failing, the NFO write failing,
the NFO saver on or off, locked fields and a locked item.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import logging
import os
import random
import threading
import unittest
from pathlib import Path
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from tmp_dirs import temp_dir
from web_stubs import _ensure_web_stubs

_ensure_web_stubs()

import models.database as mdb  # noqa: E402
from models.database import MatchOverride, Movie, set_setting  # noqa: E402
from services import wrong_match  # noqa: E402
from services.jellyfin import JellyfinUnavailable  # noqa: E402

SEEDS = int(os.environ.get("SEEDS", "1000"))
OLD, NEW = 900001, 900002
LABEL = "Label Film (1990)"
NEW_FILM = {"tmdb_id": NEW, "title": "Right Film", "year": "2020", "overview": "The right one.",
            "runtime": 83, "rating": 7.1, "genres": ["Drama"], "poster_path": "/p.jpg", "backdrop_path": None}
OLD_NFO = (b'<?xml version="1.0" encoding="UTF-8"?>\n<movie>\n  <title>Label Film</title>\n'
           b'  <tmdbid>900001</tmdbid>\n  <tag>Netflix Movies</tag>\n  <tag>Old List</tag>\n'
           b'  <dateadded>2026-01-01 10:00:00</dateadded>\n</movie>')


class FakeJellyfin:
    """One movie item for the .strm, as Jellyfin 10.11.8 answers. `saver`:
    the library's NFO saver writes movie.nfo when the item is updated."""

    def __init__(self, folder, strm_name):
        self.folder = folder
        self.user_id = "u1"
        self.item = {"Id": "jf1", "Name": "Label Film", "OriginalTitle": "Label Film", "Overview": "old",
                     "Path": f"/vod-movies/{folder.name}/{strm_name}", "ProviderIds": {"Tmdb": str(OLD), "Imdb": "tt1"},
                     "Tags": ["Netflix Movies", "Old List", "Kids OK"], "Genres": ["Comedy"],
                     "Studios": [{"Name": "Old Studio"}], "People": [{"Name": "Old Actor"}],
                     "OfficialRating": "G", "CustomRating": "Family", "CriticRating": 80,
                     "ForcedSortName": "Old sort", "ProductionLocations": ["Oldland"], "LockData": False,
                     "LockedFields": [], "DateCreated": "2026-01-01T10:00:00.0000000Z", "ProductionYear": 1990,
                     "ImageTags": {"Primary": "old-poster"}}
        self.exists = True
        self.saver = False
        self.fail = {}           # call -> "down" | "fail" | "lost"
        self.updates = []
        self.refreshes = []
        self.scans = 0

    def _maybe(self, call):
        mode = self.fail.get(call)
        if mode == "down":
            raise JellyfinUnavailable(f"{call}: timed out")
        return mode

    def get_item_strict(self, item_id):
        self._maybe("get")
        return dict(self.item, ProviderIds=dict(self.item["ProviderIds"])) if self.exists and item_id == "jf1" else None

    def movie_paths_strict(self):
        self._maybe("list")
        return [{"Id": "jf1", "Path": self.item["Path"]}] if self.exists else []

    def update_item(self, item_id, payload):
        mode = self._maybe("update")
        if mode == "fail":
            raise JellyfinUnavailable("HTTP 500")
        self.updates.append(payload)
        self.item.update({k: v for k, v in payload.items() if k != "Id"})
        if self.saver:
            (self.folder / "movie.nfo").write_text(f"<movie><tmdbid>{payload['ProviderIds'].get('Tmdb')}</tmdbid></movie>")
        if mode == "lost":
            raise JellyfinUnavailable("read timed out")

    def refresh_item_identity(self, item_id):
        """Queued in Jellyfin: it downloads the NFO's / TMDB's images, which
        no ItemUpdate touches (10.11.8 ItemUpdateController never sets
        ImageInfos), so an undo can't take them back."""
        self.refreshes.append(item_id)
        mode = self.fail.get("refresh")
        if mode in (None, "lost"):
            self.item["ImageTags"] = {"Primary": "poster-of-tmdb-" + self.item["ProviderIds"].get("Tmdb", "")}
        return mode is None                    # never raises: any failure reads False

    def trigger_library_scan(self, *a):
        self.scans += 1
        return True


class _Base(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        self.tmp = Path(temp_dir(self))
        engine = create_engine(f"sqlite:///{self.tmp}/t.db", connect_args={"check_same_thread": False})
        mdb.Base.metadata.create_all(engine)
        self.Session = sessionmaker(bind=engine)
        self.db = self.Session()
        self.addCleanup(self.db.close)
        set_setting(self.db, "jellyfin_url", "http://jf")
        set_setting(self.db, "jellyfin_api_key", "k")
        self.folder = self.tmp / "vod" / "movies" / LABEL
        self.folder.mkdir(parents=True)
        self.strm = self.folder / f"{LABEL}.strm"
        self.strm.write_text("http://p/movie/u/p/5001.mp4")
        self.nfo = self.strm.with_suffix(".nfo")
        self.nfo.write_bytes(OLD_NFO)
        self.db.add(Movie(tmdb_id=OLD, title="Label Film", year="1990", source="provider_1", provider_id=1,
                          strm_path=str(self.strm), nfo_path=str(self.nfo), tags=["Netflix Movies", "Old List"],
                          source_tag="Netflix", jellyfin_item_id="jf1"))
        self.db.commit()
        self.jf = FakeJellyfin(self.folder, self.strm.name)

        class T:
            _tl = threading.local()

            def get_movie_details(self, tid):
                return dict(NEW_FILM) if tid == NEW else None

            def _lookup_failed(self):
                return False
        for p in (mock.patch.object(wrong_match, "_tmdb", lambda db: T()),
                  mock.patch.object(wrong_match, "_jf", lambda db: self.jf),
                  mock.patch("services.tagger.get_list_tags_for_tmdb_id",
                             lambda tid, mt, db: ["Old List"] if tid == OLD else []),
                  mock.patch("services.tagger.apply_tag_rules", lambda *a: []),
                  mock.patch("services.tagger.tentacle_owned_tags", lambda db: {"Netflix Movies", "Old List"}),
                  mock.patch.object(wrong_match, "_refresh_caches", lambda: None)):
            p.start()
            self.addCleanup(p.stop)

    def fix(self):
        return wrong_match.rematch_movie(self.db, OLD, NEW, user_name="admin")

    def unchanged(self):
        self.db.expire_all()
        self.assertIsNotNone(self.db.query(Movie).filter_by(tmdb_id=OLD).first())
        self.assertIsNone(self.db.query(Movie).filter_by(tmdb_id=NEW).first())
        self.assertEqual(0, self.db.query(MatchOverride).count())
        self.assertEqual(OLD_NFO, self.nfo.read_bytes())
        self.assertEqual(str(OLD), self.jf.item["ProviderIds"]["Tmdb"])


class TestIdentity(_Base):
    def test_the_item_gets_the_right_films_identity_and_keeps_its_id(self):
        r = self.fix()
        self.assertTrue(r["ok"] and r["in_place"])
        body = self.jf.updates[0]
        self.assertEqual({"Tmdb": str(NEW)}, body["ProviderIds"], "no IMDb id of the old film")
        self.assertEqual(("Right Film", "Right Film", 2020), (body["Name"], body["OriginalTitle"], body["ProductionYear"]))
        self.assertEqual(("", [], [], [], None, None),
                         (body["OfficialRating"], body["Studios"], body["People"], body["ProductionLocations"],
                          body["ForcedSortName"], body["CriticRating"]))
        self.assertEqual("Family", body["CustomRating"], "the admin's own rating is about the stream: kept")
        self.assertEqual(["Kids OK", "Netflix Movies"], body["Tags"], "Tentacle's tags replaced, the admin's kept")
        self.assertFalse(body["LockData"])
        self.assertEqual(["jf1"], self.jf.refreshes)
        row = self.db.query(Movie).filter_by(tmdb_id=NEW).one()
        self.assertEqual((str(self.strm), "jf1"), (row.strm_path, row.jellyfin_item_id))
        self.assertIn(b"<tmdbid>900002</tmdbid>", self.nfo.read_bytes())

    def test_dateadded_is_kept(self):
        self.fix()
        self.assertIn(b"<dateadded>2026-01-01 10:00:00</dateadded>", self.nfo.read_bytes())
        self.assertEqual(1, self.nfo.read_bytes().count(b"<dateadded>"))

    def test_no_dateadded_before_means_none_now(self):
        self.nfo.write_bytes(OLD_NFO.replace(b"  <dateadded>2026-01-01 10:00:00</dateadded>\n", b""))
        self.fix()
        self.assertNotIn(b"<dateadded>", self.nfo.read_bytes())

    def test_a_locked_rating_is_never_cleared(self):
        self.jf.item["LockedFields"] = ["OfficialRating", "Tags"]
        self.jf.item["OfficialRating"] = "R"
        self.fix()
        body = self.jf.updates[0]
        self.assertEqual("R", body["OfficialRating"])
        self.assertEqual(["Netflix Movies", "Old List", "Kids OK"], body["Tags"])

    def test_a_locked_item_changes_nothing(self):
        self.jf.item["LockData"] = True
        with self.assertRaises(wrong_match.WrongMatchError) as e:
            self.fix()
        self.assertEqual(409, e.exception.status)
        self.assertEqual([], self.jf.updates)
        self.unchanged()

    def test_a_movie_nfo_of_this_film_is_the_copys_own(self):
        """The NFO saver's movie.nfo: the copy still stays where it is."""
        (self.folder / "movie.nfo").write_text("<movie><tmdbid>900001</tmdbid></movie>")
        r = self.fix()
        self.assertTrue(r["in_place"])
        self.assertTrue(self.strm.exists())


class TestNothingChangesWhenJellyfinCantBeTold(_Base):
    def test_jellyfin_down(self):
        self.jf.fail["list"] = "down"
        self.jf.fail["get"] = "down"
        with self.assertRaises(wrong_match.WrongMatchError) as e:
            self.fix()
        self.assertEqual(502, e.exception.status)
        self.unchanged()

    def test_the_update_fails(self):
        self.jf.saver = True
        self.jf.fail["update"] = "fail"
        with self.assertRaises(wrong_match.WrongMatchError) as e:
            self.fix()
        self.assertEqual(502, e.exception.status)
        self.unchanged()
        self.assertFalse((self.folder / "movie.nfo").exists())

    def test_a_lost_reply_is_read_back(self):
        self.jf.fail["update"] = "lost"
        r = self.fix()
        self.assertTrue(r["ok"])
        self.assertIsNotNone(self.db.query(Movie).filter_by(tmdb_id=NEW).first())

    def test_the_nfo_cannot_be_written(self):
        with mock.patch("services.nfo.write_movie_nfo", return_value=False):
            with self.assertRaises(wrong_match.WrongMatchError) as e:
                self.fix()
        self.assertEqual(500, e.exception.status)
        self.assertEqual([], self.jf.updates)
        self.unchanged()

    def test_the_save_fails_after_jellyfin_took_it(self):
        self.jf.saver = True
        with mock.patch.object(wrong_match, "_apply_rematch", side_effect=RuntimeError("database is locked")):
            with self.assertRaises(wrong_match.WrongMatchError) as e:
                self.fix()
        self.assertEqual(500, e.exception.status)
        self.assertEqual(2, len(self.jf.updates), "the new identity, then the old one back")
        self.assertEqual({"Tmdb": str(OLD), "Imdb": "tt1"}, self.jf.updates[1]["ProviderIds"])
        self.assertEqual("G", self.jf.updates[1]["OfficialRating"])
        self.unchanged()
        self.assertFalse((self.folder / "movie.nfo").exists())

    def test_a_failed_save_sends_no_refresh(self):
        """The refresh is queued only once the fix is saved: an earlier one
        downloads the new film's poster, and putting the identity back can't
        undo that, so the old film would show the new film's artwork."""
        with mock.patch.object(wrong_match, "_apply_rematch", side_effect=RuntimeError("database is locked")):
            with self.assertRaises(wrong_match.WrongMatchError) as e:
                self.fix()
        self.assertEqual(500, e.exception.status)
        self.assertEqual([], self.jf.refreshes)
        self.assertEqual({"Primary": "old-poster"}, self.jf.item["ImageTags"])
        self.unchanged()

    def test_a_failed_save_sends_no_scan(self):
        """Not in Jellyfin yet: no scan may read the new NFO before it is put back."""
        self.jf.exists = False
        with mock.patch.object(wrong_match, "_apply_rematch", side_effect=RuntimeError("database is locked")):
            with self.assertRaises(wrong_match.WrongMatchError):
                self.fix()
        self.assertEqual(0, self.jf.scans)
        self.assertEqual(OLD_NFO, self.nfo.read_bytes())

    def test_a_failed_refresh_still_saves_and_says_so(self):
        self.jf.fail["refresh"] = "fail"
        r = self.fix()
        self.assertTrue(r["ok"])
        self.assertIn("next metadata refresh", r["message"])
        self.assertEqual(1, self.db.query(mdb.ActivityLog).filter_by(event="fix_it_refresh_failed").count())


class TestWithoutTheItem(_Base):
    def test_not_in_jellyfin_yet_the_scan_reads_the_nfo(self):
        self.jf.exists = False
        r = self.fix()
        self.assertTrue(r["ok"])
        self.assertEqual(1, self.jf.scans)
        self.assertIn("next library scan", r["message"])

    def test_jellyfin_not_configured(self):
        with mock.patch.object(wrong_match, "_jf", lambda db: None):
            r = self.fix()
        self.assertTrue(r["ok"])
        self.assertEqual([], self.jf.updates)


class TestOneAtATime(_Base):
    def test_two_fixes_of_one_copy_at_once(self):
        """Two tabs: the second waits, then finds the copy already fixed."""
        results = []
        entered = threading.Event()
        real = self.jf.update_item

        def slow(item_id, payload):
            entered.set()
            threading.Event().wait(0.3)
            real(item_id, payload)
        self.jf.update_item = slow

        def run():
            s = self.Session()
            try:
                results.append(wrong_match.rematch_movie(s, OLD, NEW, user_name="b")["ok"])
            except wrong_match.WrongMatchError as e:
                results.append(e.status)
            finally:
                s.close()
        t = threading.Thread(target=run)
        first = threading.Thread(target=lambda: results.append(self.fix()["ok"]))
        first.start()
        entered.wait(5)
        t.start()
        first.join(10)
        t.join(10)
        self.assertEqual(sorted([True, 404], key=str), sorted(results, key=str))
        self.assertEqual(1, len(self.jf.updates))
        self.assertEqual(1, self.db.query(MatchOverride).count())


class TestProperty(_Base):
    """All or nothing, whatever fails where."""

    def test_random_faults(self):
        bad = []
        base_setup = self.setUp
        for seed in range(SEEDS):
            rnd = random.Random(seed)
            if seed:
                self.doCleanups()
                base_setup()
            jf = self.jf
            jf.saver = rnd.random() < 0.4
            if jf.saver and rnd.random() < 0.6:
                (self.folder / "movie.nfo").write_text("<movie><tmdbid>900001</tmdbid></movie>")
            jf.exists = rnd.random() < 0.85
            for call in ("list", "get", "update", "refresh"):
                r = rnd.random()
                if r < 0.15:
                    jf.fail[call] = rnd.choice(["down", "fail", "lost"])
            if rnd.random() < 0.3:
                jf.item["LockedFields"] = rnd.sample(["OfficialRating", "Tags", "Name", "Cast"], rnd.randint(1, 2))
            jf.item["LockData"] = rnd.random() < 0.1
            has_date = rnd.random() < 0.8
            if not has_date:
                self.nfo.write_bytes(OLD_NFO.replace(b"  <dateadded>2026-01-01 10:00:00</dateadded>\n", b""))
            before_nfos = {f: f.read_bytes() for f in self.folder.glob("*.nfo")}
            before_item = dict(jf.item, ProviderIds=dict(jf.item["ProviderIds"]))
            save_fails = rnd.random() < 0.1
            nfo_fails = rnd.random() < 0.05
            patches = []
            if save_fails:
                patches.append(mock.patch.object(wrong_match, "_apply_rematch", side_effect=RuntimeError("locked")))
            if nfo_fails:
                patches.append(mock.patch("services.nfo.write_movie_nfo", return_value=False))
            for p in patches:
                p.start()
            try:
                result, status = self.fix(), None
            except wrong_match.WrongMatchError as e:
                result, status = None, e.status
            finally:
                for p in patches:
                    p.stop()
            self.db.expire_all()
            ctx = f"seed {seed} (saver={jf.saver} exists={jf.exists} fail={jf.fail} lock={jf.item['LockData']} " \
                  f"locked={before_item['LockedFields']} save_fails={save_fails} nfo_fails={nfo_fails})"
            now_nfos = {f: f.read_bytes() for f in self.folder.glob("*.nfo")}
            if result is None:
                if self.db.query(Movie).filter_by(tmdb_id=OLD).first() is None or self.db.query(MatchOverride).count():
                    bad.append(f"{ctx}: failed ({status}) but Tentacle changed")
                if now_nfos != before_nfos:
                    bad.append(f"{ctx}: failed ({status}) but the NFOs changed: {sorted(p.name for p in now_nfos)}")
                if jf.item["ProviderIds"] != before_item["ProviderIds"] or jf.item["OfficialRating"] != before_item["OfficialRating"]:
                    bad.append(f"{ctx}: failed ({status}) but Jellyfin changed")
                if jf.item["ImageTags"] != before_item["ImageTags"] or jf.refreshes or jf.scans:
                    bad.append(f"{ctx}: failed ({status}) but a refresh/scan reached Jellyfin "
                               f"(images {jf.item['ImageTags']})")
                continue
            row = self.db.query(Movie).filter_by(tmdb_id=NEW).first()
            if row is None or row.strm_path != str(self.strm) or not self.db.query(MatchOverride).count():
                bad.append(f"{ctx}: ok but Tentacle not fixed")
                continue
            data = self.nfo.read_bytes()
            if b"<tmdbid>900002</tmdbid>" not in data:
                bad.append(f"{ctx}: ok but the NFO names another film")
            if (b"<dateadded>2026-01-01 10:00:00</dateadded>" in data) != has_date or data.count(b"<dateadded>") > 1:
                bad.append(f"{ctx}: <dateadded> not kept as it was")
            if jf.exists:
                if jf.item["ProviderIds"] != {"Tmdb": str(NEW)}:
                    bad.append(f"{ctx}: ok but Jellyfin shows {jf.item['ProviderIds']}")
                for field, key in (("OfficialRating", "OfficialRating"), ("Tags", "Tags"), ("Name", "Name")):
                    if field in before_item["LockedFields"] and jf.item[key] != before_item[key]:
                        bad.append(f"{ctx}: locked {field} changed")
                if "OfficialRating" not in before_item["LockedFields"] and jf.item["OfficialRating"]:
                    bad.append(f"{ctx}: the old film's rating stayed")
                if jf.item["Id"] != "jf1" or row.jellyfin_item_id != "jf1":
                    bad.append(f"{ctx}: item id changed")
        print(f"\n#294 fix-it property: {SEEDS} seeds, {len(bad)} violations")
        for b in bad[:5]:
            print("  ", b)
        self.assertEqual([], bad)


if __name__ == "__main__":
    unittest.main()
