"""Mislabelled provider streams: "Wrong movie" blocks + removes; a runtime check flags.

Found live: provider stream 188327 is listed as "The Decline of Western
Civilization" (1981) but plays "The Decline" (2020). Tentacle trusted the label,
showed the documentary as In Library, and hid the user's Radarr request for it.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import tempfile
import unittest
from pathlib import Path as _RealPath
from unittest import mock

from web_stubs import _ensure_web_stubs  # noqa: E402

_ensure_web_stubs()

import models.database as mdb  # noqa: E402
from models.database import BlockedStream, DownloadRequest, MatchSuspect, Movie  # noqa: E402
from nightly_harness import NightlyHarness, FakeTMDB  # noqa: E402
from services import wrong_match  # noqa: E402


class TestStreamKey(unittest.TestCase):
    def test_xtream_url_is_keyed_by_stream_id(self):
        self.assertEqual("188327", wrong_match.stream_key_for_url(
            "http://cf.example/movie/user/pass/188327.mp4\n"))
        self.assertEqual("42", wrong_match.stream_key_for_url("http://x/movie/u/p/42.mkv"))

    def test_m3u_url_is_keyed_by_the_url(self):
        url = "http://m3u.example/vod/some-film.m3u8?token=abc"
        self.assertEqual(url, wrong_match.stream_key_for_url(url))

    def test_blank(self):
        self.assertIsNone(wrong_match.stream_key_for_url("  "))

    def test_is_blocked(self):
        self.assertTrue(wrong_match.is_blocked({"7"}, 7))
        self.assertTrue(wrong_match.is_blocked({"http://u"}, 9, "http://u"))
        self.assertFalse(wrong_match.is_blocked({"7"}, 8, "http://u"))
        self.assertFalse(wrong_match.is_blocked(set(), 7))

class FakeJf:
    """Records deletes, and what Tentacle's DB looked like at that moment.

    Lists one Jellyfin item per VOD .strm it has seen (Jellyfin mounts the
    VOD folder at another prefix), plus `extra` items -- e.g. a Radarr download
    of the same film, listed FIRST as Jellyfin may well list it."""

    def __init__(self, db):
        self.db = db
        self.deleted = []
        self.row_present_at_delete = None
        self.items = {}
        self.extra = []

    def __call__(self, *a, **k):
        return self

    def sync_items(self):
        for m in self.db.query(Movie).filter(Movie.strm_path.isnot(None)).all():
            tail = "/".join(_RealPath(m.strm_path).parts[-2:])
            self.items.setdefault(f"jf-{m.tmdb_id}", {
                "Id": f"jf-{m.tmdb_id}", "ProviderIds": {"Tmdb": str(m.tmdb_id)}, "Path": "/jfvod/" + tail})

    def _all(self):
        self.sync_items()
        return list(self.extra) + list(self.items.values())

    def _get(self, path, params=None):
        items = self._all()
        start = int((params or {}).get("StartIndex") or 0)
        return {"Items": items[start:], "TotalRecordCount": len(items)}

    def get_item_by_id(self, item_id):
        return next((i for i in self._all() if i["Id"] == item_id), None)

    def trigger_library_scan(self, *a, **k):
        return True

    def search_by_tmdb_id(self, tmdb_id, media_type="Movie", **k):
        return next((i for i in self._all() if i["ProviderIds"]["Tmdb"] == str(tmdb_id)), None)

    def delete_item(self, item_id):
        tmdb = int(item_id.split("-")[-1])
        self.row_present_at_delete = self.db.query(Movie).filter(Movie.tmdb_id == tmdb).first() is not None
        self.deleted.append(item_id)
        return True


class _Base(NightlyHarness):
    def setUp(self):
        super().setUp()
        self.add_category("1", name="NETFLIX MOVIES")
        self.add_category("2", name="DOCS")
        # Enough titles that one removal never trips the prune safety limits.
        self.catalogue_movies("1", [f"Movie {i}" for i in range(60)])
        self.catalogue_movies("2", [f"Doc {i}" for i in range(60)], first_tmdb=3000)
        self.night()
        mdb.set_setting(self.db, "jellyfin_url", "http://jf")
        mdb.set_setting(self.db, "jellyfin_api_key", "k")
        self.jf = FakeJf(self.db)
        for p in (mock.patch("services.jellyfin.JellyfinService", self.jf),
                  mock.patch("routers.library._cleanup_playlists_all_users")):
            p.start()
            self.addCleanup(p.stop)
        self.tmdb = FakeTMDB.ids["Movie 9"]   # the "mislabelled" one (stream id == tmdb id here)
        self.jf.sync_items()

    def report(self, tmdb_id=None):
        return wrong_match.block_and_remove_movie(self.db, tmdb_id or self.tmdb, user_name="Lucas")


class TestWrongMovie(_Base):
    def test_removes_the_copy_and_blocks_the_stream(self):
        strm = _RealPath(self.movie(self.tmdb).strm_path)
        self.assertTrue(strm.exists())
        r = self.report()
        self.db.expire_all()
        self.assertIsNone(self.movie(self.tmdb))
        self.assertFalse(strm.exists())
        self.assertFalse(strm.with_suffix(".nfo").exists())
        self.assertEqual(str(self.tmdb), r["blocked"])
        self.assertEqual([f"jf-{self.tmdb}"], self.jf.deleted)
        b = self.db.query(BlockedStream).one()
        self.assertEqual((self.provider.id, "movie", str(self.tmdb), "Lucas"),
                         (b.provider_id, b.media_type, b.stream_key, b.blocked_by))

    def test_never_comes_back_on_later_nights(self):
        self.report()
        for _ in range(3):
            self.night()
        self.assertIsNone(self.movie(self.tmdb), "the mislabelled stream was re-imported")
        self.assertIsNotNone(self.movie(FakeTMDB.ids["Movie 8"]), "neighbours untouched")

    def test_blocked_in_every_category_it_appears_in(self):
        self.report()
        # The provider lists the same stream under a second category.
        self.client.movies["2"].append(("Movie 9", self.tmdb))
        self.night()
        self.assertIsNone(self.movie(self.tmdb))

    def test_a_correct_stream_for_the_same_film_still_imports(self):
        self.report()
        self.client.movies["2"].append(("Movie 9", 99999))   # a different, genuine stream
        self.night()
        row = self.movie(self.tmdb)
        self.assertIsNotNone(row)
        self.assertIn("99999", _RealPath(row.strm_path).read_text())

    def test_unblocking_lets_it_back_in(self):
        self.report()
        self.db.query(BlockedStream).delete()
        self.db.commit()
        self.night()
        self.assertIsNotNone(self.movie(self.tmdb))

    def test_the_request_for_the_real_film_survives(self):
        """Radarr is searching for the REAL film; its request must not be lost.
        The Jellyfin delete fires the plugin's clean-up, which drops requests
        for any title still in Tentacle's DB — so the row goes first."""
        admin = mdb.TentacleUser(jellyfin_user_id="a" * 32, display_name="Lucas", is_admin=True)
        self.db.add(admin)
        self.db.commit()
        self.db.add(DownloadRequest(tmdb_id=self.tmdb, media_type="movie", user_id=admin.id))
        self.db.commit()
        self.report()
        self.assertFalse(self.jf.row_present_at_delete, "Jellyfin item deleted before Tentacle's row")
        self.assertEqual(1, self.db.query(DownloadRequest).count())

    def test_reporting_twice_does_not_duplicate_the_block(self):
        self.report()
        self.night()
        self.client.movies["1"] = [(t, s) for t, s in self.client.movies["1"]]  # unchanged
        with self.assertRaises(wrong_match.WrongMatchError) as e:
            self.report()  # already gone
        self.assertEqual(404, e.exception.status)
        self.assertEqual(1, self.db.query(BlockedStream).count())

    def test_a_download_cannot_be_reported(self):
        self.db.add(Movie(tmdb_id=424242, title="Downloaded", source="radarr"))
        self.db.commit()
        with self.assertRaises(wrong_match.WrongMatchError) as e:
            self.report(424242)
        self.assertEqual(400, e.exception.status)

    def test_without_its_strm_it_cannot_be_blocked_and_nothing_is_removed(self):
        _RealPath(self.movie(self.tmdb).strm_path).unlink()
        with self.assertRaises(wrong_match.WrongMatchError) as e:
            self.report()
        self.assertEqual(409, e.exception.status)
        self.assertIsNotNone(self.movie(self.tmdb))
        self.assertEqual(0, self.db.query(BlockedStream).count())

    def test_a_download_of_the_same_film_is_never_deleted_in_jellyfin(self):
        """The same film is in Jellyfin twice: a Radarr download (listed first)
        and this IPTV copy. Deleting "the first movie with this TMDB id" through
        Jellyfin deleted the DOWNLOAD's folder from disk."""
        self.jf.extra = [{"Id": f"dl-{self.tmdb}", "ProviderIds": {"Tmdb": str(self.tmdb)},
                          "Path": "/downloads/Movie 9 (2020)/Movie 9 (2020).mkv"}]
        self.report()
        self.assertEqual([f"jf-{self.tmdb}"], self.jf.deleted)

    def test_a_stored_id_of_the_download_is_not_trusted(self):
        """Discover backfills jellyfin_item_id from a TMDB lookup -- it can be
        the download's id."""
        self.jf.extra = [{"Id": f"dl-{self.tmdb}", "ProviderIds": {"Tmdb": str(self.tmdb)},
                          "Path": "/downloads/Movie 9 (2020)/Movie 9 (2020).mkv"}]
        row = self.movie(self.tmdb)
        row.jellyfin_item_id = f"dl-{self.tmdb}"
        self.db.commit()
        self.report()
        self.assertEqual([f"jf-{self.tmdb}"], self.jf.deleted)

    def test_no_item_for_this_strm_deletes_nothing(self):
        self.jf.items.clear()
        self.jf.sync_items = lambda: None
        self.jf.extra = [{"Id": f"dl-{self.tmdb}", "ProviderIds": {"Tmdb": str(self.tmdb)},
                          "Path": "/downloads/Movie 9 (2020)/Movie 9 (2020).mkv"}]
        r = self.report()
        self.assertEqual([], self.jf.deleted)
        self.assertFalse(r["jellyfin_deleted"])
        self.assertIsNone(self.movie(self.tmdb), "Tentacle's copy is still removed")

    def test_it_is_audited(self):
        self.report()
        log = self.db.query(mdb.DeletionLog).filter(mdb.DeletionLog.kind == "wrong-match").one()
        self.assertEqual("Movie 9", log.name)
        self.assertIn(str(self.tmdb), log.detail)


class TestRuntimeCheck(_Base):
    def listing(self, **probed):
        """probed: tmdb_id -> probed minutes."""
        return [{"Id": f"jf-{t}", "ProviderIds": {"Tmdb": str(t)},
                 "MediaSources": [{"RunTimeTicks": int(m * 600_000_000)}]} for t, m in probed.items()]

    def run_check(self, items):
        self.jf.query_movies_with_media_sources = lambda: items
        return wrong_match.check_runtime_mismatches(self.db)

    def set_runtime(self, tmdb, minutes):
        self.movie(tmdb).runtime = minutes
        self.db.commit()

    def test_a_different_film_is_flagged(self):
        self.set_runtime(self.tmdb, 100)          # TMDB: the 1981 documentary
        r = self.run_check(self.listing(**{str(self.tmdb): 83}))   # the stream: 83 min
        self.assertEqual(1, r["flagged"])
        s = self.db.query(MatchSuspect).one()
        self.assertEqual((100, 83, "Movie 9"), (s.expected_minutes, s.actual_minutes, s.title))

    def test_small_differences_are_not(self):
        self.set_runtime(self.tmdb, 100)
        self.assertEqual(0, self.run_check(self.listing(**{str(self.tmdb): 92}))["flagged"])   # -8 min
        self.set_runtime(self.tmdb, 200)
        self.assertEqual(0, self.run_check(self.listing(**{str(self.tmdb): 180}))["flagged"])  # -10%

    def test_unprobed_titles_are_skipped(self):
        self.set_runtime(self.tmdb, 100)
        items = [{"Id": "x", "ProviderIds": {"Tmdb": str(self.tmdb)}, "MediaSources": [{}]}]
        self.assertEqual(0, self.run_check(items)["checked"])

    def test_a_dismissed_flag_stays_dismissed(self):
        self.set_runtime(self.tmdb, 100)
        self.run_check(self.listing(**{str(self.tmdb): 83}))
        self.db.query(MatchSuspect).update({"dismissed": True})
        self.db.commit()
        self.assertEqual(0, self.run_check(self.listing(**{str(self.tmdb): 83}))["flagged"])
        self.assertTrue(self.db.query(MatchSuspect).one().dismissed)

    def test_a_flag_that_no_longer_applies_is_cleared(self):
        self.set_runtime(self.tmdb, 100)
        self.run_check(self.listing(**{str(self.tmdb): 83}))
        self.run_check(self.listing(**{str(self.tmdb): 99}))
        self.assertEqual(0, self.db.query(MatchSuspect).count())

    def test_a_failed_listing_changes_nothing(self):
        self.set_runtime(self.tmdb, 100)
        self.run_check(self.listing(**{str(self.tmdb): 83}))
        r = self.run_check(None)
        self.assertIn("error", r)
        self.assertEqual(1, self.db.query(MatchSuspect).count())

    def test_reporting_a_flagged_title_clears_its_flag(self):
        self.set_runtime(self.tmdb, 100)
        self.run_check(self.listing(**{str(self.tmdb): 83}))
        self.report()
        self.assertEqual(0, self.db.query(MatchSuspect).count())

    def test_downloads_are_not_checked(self):
        self.db.add(Movie(tmdb_id=777, title="Download", source="radarr", runtime=100))
        self.db.commit()
        self.assertEqual(0, self.run_check(self.listing(**{"777": 50}))["checked"])


if __name__ == "__main__":
    unittest.main()


# ── Fixing the match instead of removing ─────────────────────────────────────

REAL = {"tmdb_id": 674607, "title": "The Decline", "year": "2020", "overview": "Survivalists...",
        "runtime": 83, "rating": 5.5, "genres": ["Thriller"], "poster_path": "/decline.jpg",
        "backdrop_path": None}


NO_PROBE = {"minutes": None, "audio_languages": []}


class FakeRealTMDB:
    """TMDB for the fixer: search results + details, like the live case."""
    def __init__(self):
        self.searches = []

    def get_movie_details(self, tmdb_id):
        if tmdb_id == REAL["tmdb_id"]:
            return dict(REAL)
        return {21137: {"tmdb_id": 21137, "title": "The Decline of Western Civilization", "year": "1981", "runtime": 100},
                44848: {"tmdb_id": 44848, "title": "The Decline of Western Civilization Part III", "year": "1998", "runtime": 86},
                36724: {"tmdb_id": 36724, "title": "The Decline of Western Civilization Part II", "year": "1988", "runtime": 93},
                }.get(tmdb_id)

    def _request(self, endpoint, params):
        self.searches.append(params["query"])
        pool = [{"id": 36724, "title": "The Decline of Western Civilization Part II", "release_date": "1988-06-17",
                 "popularity": 5, "original_language": "en"},
                {"id": 44848, "title": "The Decline of Western Civilization Part III", "release_date": "1998-11-13",
                 "popularity": 4, "original_language": "en"}]
        if params["query"] == "The Decline":
            pool = [{"id": 674607, "title": "The Decline", "release_date": "2020-02-27", "popularity": 9,
                     "original_language": "fr"}] + pool
        return {"results": pool}

    @staticmethod
    def _similarity(a, b):
        from difflib import SequenceMatcher
        return SequenceMatcher(None, a.lower(), b.lower()).ratio()


class TestSuggestions(_Base):
    def setUp(self):
        super().setUp()
        self.fake = FakeRealTMDB()
        row = self.movie(self.tmdb)
        row.title = "The Decline of Western Civilization"
        self.db.commit()
        for p in (mock.patch.object(wrong_match, "_tmdb", lambda db: self.fake),):
            p.start()
            self.addCleanup(p.stop)

    def test_the_real_film_comes_first_when_the_length_is_known(self):
        with mock.patch.object(wrong_match, "probe_info", return_value={"minutes": 83, "audio_languages": []}):
            r = wrong_match.suggest_matches(self.db, self.tmdb)
        self.assertEqual(83, r["actual_minutes"])
        self.assertEqual(674607, r["candidates"][0]["tmdb_id"])
        self.assertTrue(r["candidates"][0]["runtime_matches"])
        self.assertIn("The Decline", r["searched"], "shortens the label until it finds candidates")

    def test_a_search_replaces_the_label(self):
        with mock.patch.object(wrong_match, "probe_info", return_value=NO_PROBE):
            wrong_match.suggest_matches(self.db, self.tmdb, query="The Decline")
        self.assertEqual(["The Decline"], self.fake.searches)

    def test_the_audio_language_is_a_clue_when_the_length_is_unknown(self):
        with mock.patch.object(wrong_match, "probe_info", return_value={"minutes": None, "audio_languages": ["fr"]}):
            r = wrong_match.suggest_matches(self.db, self.tmdb)
        top = r["candidates"][0]
        self.assertEqual(674607, top["tmdb_id"], "the French film beats closer-titled English ones")
        self.assertTrue(top["language_matches"])
        self.assertEqual("French", top["language_name"])
        self.assertEqual([{"code": "fr", "name": "French"}], r["audio_languages"])

    def test_without_a_language_the_label_decides(self):
        with mock.patch.object(wrong_match, "probe_info", return_value=NO_PROBE):
            r = wrong_match.suggest_matches(self.db, self.tmdb)
        self.assertEqual(36724, r["candidates"][0]["tmdb_id"])
        self.assertFalse(any(c["language_matches"] for c in r["candidates"]))

    def test_a_dubbed_multi_language_stream_is_no_clue(self):
        with mock.patch.object(wrong_match, "probe_info", return_value={"minutes": None, "audio_languages": ["en", "fr"]}):
            r = wrong_match.suggest_matches(self.db, self.tmdb)
        self.assertEqual(36724, r["candidates"][0]["tmdb_id"])

    def test_length_still_outranks_language(self):
        with mock.patch.object(wrong_match, "probe_info", return_value={"minutes": 93, "audio_languages": ["fr"]}):
            r = wrong_match.suggest_matches(self.db, self.tmdb)
        self.assertEqual(36724, r["candidates"][0]["tmdb_id"], "Part II is 93 min")

    def test_the_current_film_is_never_suggested(self):
        with mock.patch.object(wrong_match, "probe_info", return_value=NO_PROBE):
            r = wrong_match.suggest_matches(self.db, self.tmdb)
        self.assertNotIn(self.tmdb, [c["tmdb_id"] for c in r["candidates"]])


class TestLanguageCodes(unittest.TestCase):
    def test_jellyfin_and_tmdb_codes_meet(self):
        for jf, tmdb in (("fre", "fr"), ("fra", "fr"), ("eng", "en"), ("ger", "de"), ("jpn", "ja"), ("en", "en")):
            self.assertEqual(tmdb, wrong_match.language_code(jf))
        for unknown in ("und", "", None, "mul", "zzz"):
            self.assertIsNone(wrong_match.language_code(unknown))


class TestProbeInfo(_Base):
    def test_reads_length_and_audio_languages(self):
        self.jf.get_item_by_id = lambda _id: {"MediaSources": [{
            "RunTimeTicks": 83 * 600_000_000,
            "MediaStreams": [{"Type": "Video"}, {"Type": "Audio", "Language": "fre"},
                             {"Type": "Audio", "Language": "fra"}, {"Type": "Subtitle", "Language": "eng"},
                             {"Type": "Audio", "Language": "und"}]}]}
        info = wrong_match.probe_info(self.db, self.movie(self.tmdb))
        self.assertEqual({"minutes": 83, "audio_languages": ["fr"]}, info)

    def test_never_played_has_nothing(self):
        self.jf.get_item_by_id = lambda _id: {"MediaSources": []}
        self.assertEqual(NO_PROBE, wrong_match.probe_info(self.db, self.movie(self.tmdb)))


class TestFrames(_Base):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp()
        self.grabs = []

        def grab(ffmpeg, url, ua, sec):
            self.grabs.append((url, ua, sec))
            return b"JPEG" if self.ok else None
        self.ok = True
        for p in (mock.patch.dict("os.environ", {"DATA_DIR": self.tmp}),
                  mock.patch("shutil.which", return_value="/usr/bin/ffmpeg"),
                  mock.patch.object(wrong_match, "_grab_frame", side_effect=grab),
                  mock.patch.object(wrong_match, "probed_minutes", return_value=100)):
            p.start()
            self.addCleanup(p.stop)

    def test_three_stills_spread_over_the_film(self):
        r = wrong_match.stream_frames(self.db, self.tmdb)
        self.assertEqual([720, 2400, 4200], [g[2] for g in self.grabs])
        self.assertEqual([12, 40, 70], [f["at_minutes"] for f in r["frames"]])
        self.assertTrue(r["frames"][0]["image"].startswith("data:image/jpeg;base64,"))
        url = _RealPath(self.movie(self.tmdb).strm_path).read_text().strip()
        self.assertEqual(url, self.grabs[0][0], "grabs from the title's own stream")

    def test_cached_the_second_time(self):
        wrong_match.stream_frames(self.db, self.tmdb)
        wrong_match.stream_frames(self.db, self.tmdb)
        self.assertEqual(3, len(self.grabs))

    def test_some_frames_are_better_than_none(self):
        calls = iter([b"A", None, b"C"])
        wrong_match._grab_frame.side_effect = lambda *a: next(calls)
        r = wrong_match.stream_frames(self.db, self.tmdb)
        self.assertEqual(2, len(r["frames"]))

    def test_a_busy_provider_is_a_clear_error(self):
        self.ok = False
        with self.assertRaises(wrong_match.WrongMatchError) as e:
            wrong_match.stream_frames(self.db, self.tmdb)
        self.assertEqual(502, e.exception.status)
        self.assertEqual(0, len(list(_RealPath(self.tmp).glob("frame_cache/*.jpg"))), "failures aren't cached")

    def test_no_ffmpeg(self):
        with mock.patch("shutil.which", return_value=None), \
                self.assertRaises(wrong_match.WrongMatchError) as e:
            wrong_match.stream_frames(self.db, self.tmdb)
        self.assertEqual(503, e.exception.status)

    def test_offsets_without_a_length(self):
        self.assertEqual([300, 1200, 2700], wrong_match.frame_offsets(None))


class TestRematch(_Base):
    def setUp(self):
        super().setUp()
        self.fake = FakeRealTMDB()
        p = mock.patch.object(wrong_match, "_tmdb", lambda db: self.fake)
        p.start()
        self.addCleanup(p.stop)
        # The sync fetches details for a re-matched stream it has no row for.
        FakeTMDB.get_movie_details = lambda _self, tid: self.fake.get_movie_details(tid)
        self.addCleanup(lambda: delattr(FakeTMDB, "get_movie_details"))
        self.jf.trigger_library_scan = mock.Mock(return_value=True)

    def rematch(self, new=674607):
        return wrong_match.rematch_movie(self.db, self.tmdb, new, user_name="Lucas")

    def test_the_copy_moves_to_the_right_film(self):
        old = self.movie(self.tmdb)
        old_strm = _RealPath(old.strm_path)
        url = old_strm.read_text()
        r = self.rematch()
        self.db.expire_all()
        self.assertTrue(r["ok"])
        self.assertIsNone(self.movie(self.tmdb))
        row = self.movie(674607)
        self.assertEqual(("The Decline", "2020", 83), (row.title, row.year, row.runtime))
        new_strm = _RealPath(row.strm_path)
        self.assertEqual("The Decline (2020)", new_strm.parent.name)
        self.assertEqual(url, new_strm.read_text(), "same stream, new identity")
        self.assertIn("<title>The Decline</title>", new_strm.with_suffix(".nfo").read_text())
        self.assertFalse(old_strm.exists())
        self.assertIn("Tag1 Movies", row.tags, "source category tags are kept")
        self.jf.trigger_library_scan.assert_called_once()

    def test_it_stays_fixed_every_night(self):
        self.rematch()
        for _ in range(3):
            self.night()
        self.assertIsNotNone(self.movie(674607))
        self.assertIsNone(self.movie(self.tmdb), "the wrong label came back as a new title")
        self.assertEqual(1, self.db.query(Movie).filter(Movie.title == "The Decline").count())

    def test_if_the_copy_is_lost_the_sync_rebuilds_it_as_the_right_film(self):
        self.rematch()
        self.db.query(Movie).filter(Movie.tmdb_id == 674607).delete()
        self.db.commit()
        self.night()
        row = self.movie(674607)
        self.assertIsNotNone(row)
        self.assertEqual("The Decline", row.title)
        self.assertIsNone(self.movie(self.tmdb))

    def test_a_download_of_the_labelled_film_survives_in_jellyfin(self):
        self.jf.extra = [{"Id": f"dl-{self.tmdb}", "ProviderIds": {"Tmdb": str(self.tmdb)},
                          "Path": "/downloads/Movie 9 (2020)/Movie 9 (2020).mkv"}]
        self.rematch()
        self.assertEqual([f"jf-{self.tmdb}"], self.jf.deleted)

    def test_the_request_for_the_labelled_film_survives(self):
        admin = mdb.TentacleUser(jellyfin_user_id="a" * 32, display_name="Lucas", is_admin=True)
        self.db.add(admin)
        self.db.commit()
        self.db.add(DownloadRequest(tmdb_id=self.tmdb, media_type="movie", user_id=admin.id))
        self.db.commit()
        self.rematch()
        self.assertFalse(self.jf.row_present_at_delete)
        self.assertEqual(1, self.db.query(DownloadRequest).filter(DownloadRequest.tmdb_id == self.tmdb).count())

    def test_the_right_film_already_in_the_library_means_this_is_a_duplicate(self):
        other = FakeTMDB.ids["Movie 8"]
        r = wrong_match.rematch_movie(self.db, self.tmdb, other, user_name="Lucas")
        self.assertTrue(r["merged"])
        self.assertIsNone(self.movie(self.tmdb))
        self.assertIsNotNone(self.movie(other))
        self.assertEqual(1, self.db.query(BlockedStream).count())

    def test_downloads_cannot_be_rematched(self):
        self.db.add(Movie(tmdb_id=424242, title="Downloaded", source="radarr"))
        self.db.commit()
        with self.assertRaises(wrong_match.WrongMatchError) as e:
            wrong_match.rematch_movie(self.db, 424242, 674607)
        self.assertEqual(400, e.exception.status)

    def test_it_is_audited(self):
        self.rematch()
        log = self.db.query(mdb.DeletionLog).filter(mdb.DeletionLog.kind == "rematch").one()
        self.assertIn("674607", log.detail)
