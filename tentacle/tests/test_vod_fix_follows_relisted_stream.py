"""A fixed or blocked stream the provider re-lists under a new id (N1).

"Fix it" pins a stream to the right film with a MatchOverride, "Wrong movie"
blocks it with a BlockedStream; both are keyed by the stream id. Providers
re-list uploads under a new id (#263). A fixed stream re-listed with its old
(wrong) label was matched by that label again: the label's film was imported
(the wrong film, under its parental rating), and the fixed copy -- its stream
no longer listed -- was pruned with every user's data on it. A blocked stream
came back under its new id.

Now the sync stores the label each fixed/blocked stream is listed with, holds
a listed stream of no row that carries exactly such a label until the whole
listing is known, and then:
- the fix follows it (one fixed stream with that label, its id gone from a
  complete listing, one new stream with it that no film of ours plays): the
  override moves to the new id, #263 points the copy's .strm there in place,
  and the admin gets an Activity entry and an Undo under Possible wrong movies;
- a block does not move: the stream is added and flagged for the admin;
- anything else (the old id still listed, two streams with the label, part
  of the catalogue unread) places the stream as before, or holds it one more
  night when the listing was incomplete.

The property test runs N1_PROPERTY_SEEDS random seeds (default 25; 1,000
in review). Run from tentacle/: python -m unittest discover -s tests
"""
import os
import random
import threading
import unittest
from pathlib import Path as _RealPath
from unittest import mock

import services.sync as sync
from models.database import ActivityLog, BlockedStream, MatchOverride, MatchSuspect, Movie
from services import wrong_match
from test_vod_namesakes import TMDB
from test_vod_relisted_streams import FILLER, Relisted, YearStrictTMDB
from test_vod_namesakes import stream

SEEDS = int(os.environ.get("N1_PROPERTY_SEEDS", "25"))
LABEL = "The Decline of Western Civilization (1981)"


class _Fixed(Relisted):
    def setUp(self):
        super().setUp()
        TMDB.films.update({21137: ("The Decline of Western Civilization", "1981"),
                           674607: ("The Decline", "2020")})
        TMDB.search["The Decline of Western Civilization"] = 21137
        for p in (mock.patch.object(wrong_match, "_tmdb", lambda db: TMDB()),
                  mock.patch.object(wrong_match, "_refresh_caches", lambda: None)):
            p.start()
            self.addCleanup(p.stop)

    def listing(self, *streams):
        self.catalogue(*streams)

    def fix(self):
        wrong_match.rematch_movie(self.db, 21137, 674607, user_name="admin")
        self.db.expire_all()

    def block(self):
        wrong_match.block_and_remove_movie(self.db, 21137, user_name="admin")
        self.db.expire_all()

    def override(self):
        return self.db.query(MatchOverride).one()


class TestAFixFollows(_Fixed):
    def setUp(self):
        super().setUp()
        self.listing(stream(LABEL, 188327))
        self.night()
        self.fix()
        self.path = self.strm(674607)
        self.night()                       # the label is stored while the stream is listed
        self.assertEqual(f"{LABEL}\tmp4", self.override().label)

    def test_the_fix_follows_the_relisted_stream(self):
        self.listing(stream(LABEL, 999001))
        for _ in range(3):
            self.night()
        self.assertIsNone(self.strm(21137), "the label's film came back under the wrong stream")
        self.assertEqual(self.path, self.strm(674607), "the fixed copy moved or was pruned")
        self.assertTrue(self.path.read_text().endswith("/999001.mp4"), self.path.read_text())
        ov = self.override()
        self.assertEqual(("999001", "188327"), (ov.stream_key, ov.moved_from))
        s = self.db.query(MatchSuspect).filter_by(tmdb_id=674607).one()
        self.assertEqual("relist_followed", s.reason)
        self.assertEqual(1, self.db.query(ActivityLog).filter_by(event="fix_it_followed").count())

    def test_undo(self):
        self.listing(stream(LABEL, 999001))
        self.night()
        r = wrong_match.undo_followed_fix(self.db, 674607, user_name="admin")
        self.assertTrue(r["ok"])
        self.db.expire_all()
        ov = self.override()
        self.assertEqual(("188327", None, None), (ov.stream_key, ov.moved_from, ov.label))
        self.assertTrue(self.path.read_text().endswith("/188327.mp4"))
        self.assertEqual(0, self.db.query(MatchSuspect).count())
        self.night()
        self.assertIsNotNone(self.strm(21137), "after Undo the re-listed stream is placed by its label")

    def test_while_the_old_id_is_listed_nothing_follows(self):
        self.listing(stream(LABEL, 188327), stream(LABEL, 999001))
        self.night()
        self.assertEqual("188327", self.override().stream_key)
        self.assertTrue(self.path.read_text().endswith("/188327.mp4"))

    def test_an_incomplete_listing_holds_the_stream(self):
        self.listing(stream(LABEL, 999001))
        self.client.movies["b"] = [stream("Heat (1995)", 900)]
        self.client.raise_for = {"b"}
        sync.sync_provider(self.p, "full", self.db)
        self.db.expire_all()
        self.assertEqual("188327", self.override().stream_key)
        self.assertIsNone(self.strm(21137), "held: the label's film is not imported")
        self.client.raise_for = set()
        self.client.movies.pop("b")
        self.night()
        self.assertEqual("999001", self.override().stream_key)
        self.assertIsNone(self.strm(21137))

    def test_two_streams_with_the_label_follow_nothing(self):
        self.listing(stream(LABEL, 999001), stream(LABEL, 999002))
        self.night()
        self.assertEqual("188327", self.override().stream_key)

    def test_a_new_id_another_film_plays_is_not_taken(self):
        other = self.strm(5000)          # a filler film now plays 999001
        other.write_text("http://provider/movie/u/p/999001.mp4", encoding="utf-8")
        self.catalogue(stream(LABEL, 999001))
        self.client.movies["a"] = [s for s in self.client.movies["a"] if s["stream_id"] != 5000]
        self.night()
        self.assertEqual("188327", self.override().stream_key)


class TestABlockDoesNotFollow(_Fixed):
    def test_a_relisted_blocked_stream_is_added_and_flagged(self):
        self.listing(stream(LABEL, 188327))
        self.night()
        self.block()
        self.night()                       # label stored
        self.assertEqual(f"{LABEL}\tmp4", self.db.query(BlockedStream).one().label)
        self.listing(stream(LABEL, 999001))
        self.night()
        self.assertIsNotNone(self.strm(21137))
        s = self.db.query(MatchSuspect).filter_by(tmdb_id=21137).one()
        self.assertEqual("relist_blocked", s.reason)
        b = self.db.query(BlockedStream).one()
        self.assertEqual(("188327", None), (b.stream_key, b.label))
        self.night()                        # asked once; the runtime check keeps the flag
        self.assertEqual(1, self.db.query(MatchSuspect).count())


class TestPhaseDCases(_Fixed):
    """Cases the Phase D review found (N1-D1, N1-D2, F1)."""

    def _fixed_and_labelled(self):
        self.listing(stream(LABEL, 188327))
        self.night()
        self.fix()
        self.path = self.strm(674607)
        self.night()

    def test_a_tmdb_error_on_the_relisting_night_keeps_the_blocked_label(self):
        self.listing(stream(LABEL, 188327))
        self.night()
        self.block()
        self.night()
        self.listing(stream(LABEL, 999001))
        real = YearStrictTMDB.search_movie

        def boom(self_, name, year=None, **k):
            if name.startswith("The Decline of Western"):
                raise RuntimeError("TMDB 503")
            return real(self_, name, year, **k)
        with mock.patch.object(YearStrictTMDB, "search_movie", boom):
            sync.sync_provider(self.p, "full", self.db)
            self.db.expire_all()
        self.assertEqual(f"{LABEL}\tmp4", self.db.query(BlockedStream).one().label, "still held")
        self.assertIsNone(self.strm(21137))
        self.night()
        self.assertIsNotNone(self.strm(21137))
        self.assertEqual("relist_blocked", self.db.query(MatchSuspect).filter_by(tmdb_id=21137).one().reason)

    def test_never_follows_onto_a_stream_listed_next_to_the_fixed_one(self):
        """The genuine label film's stream, owned by Radarr here, was listed
        next to the fixed upload; then the provider drops the fixed upload."""
        self._fixed_and_labelled()
        self.db.add(Movie(tmdb_id=21137, title="The Decline of Western Civilization", year="1981",
                          source="radarr", radarr_path="/media/movies/Decline/Decline.mkv"))
        self.db.commit()
        self.listing(stream(LABEL, 188327), stream(LABEL, 555))
        self.night()
        self.listing(stream(LABEL, 555))
        self.night()
        self.assertEqual("188327", self.override().stream_key)
        self.assertTrue(self.path.read_text().endswith("/188327.mp4"), "the fixed copy plays another film")
        self.assertTrue(self.override().label.startswith(sync.SHARED_LABEL))

    def test_the_same_stream_in_two_categories_is_one_stream(self):
        self._fixed_and_labelled()
        self.listing(stream(LABEL, 999001))
        self.client.movies["b"] = [stream(LABEL, 999001)]
        self.night()
        self.assertEqual("999001", self.override().stream_key)
        self.assertIsNone(self.strm(21137))


class TestSharedLabel(_Fixed):
    """The shared-label mark, including the night a label is first stored
    (the first sync after a Fix it, or after the upgrade). From the Phase D
    review, round 2."""

    def _radarr_l(self):
        self.db.add(Movie(tmdb_id=21137, title="The Decline of Western Civilization", year="1981",
                          source="radarr", radarr_path="/m/D/D.mkv"))
        self.db.commit()

    def test_twin_present_on_the_label_learning_night(self):
        """The twin is listed on the very night the label is first stored (Fix it done by day,
        or the first sync after the upgrade); the fixed stream is gone the next night."""
        self.listing(stream(LABEL, 188327)); self.night(); self.fix()
        self._radarr_l()
        self.listing(stream(LABEL, 188327), stream(LABEL, 555))
        self.night()                                   # label learned now; twin not held yet
        self.listing(stream(LABEL, 555))
        self.night()
        ov = self.override()
        self.assertEqual("188327", ov.stream_key, "followed onto a twin listed next to it on the learning night")

    def test_marker_persists_and_rename_clears(self):
        self.listing(stream(LABEL, 188327)); self.night(); self.fix(); self.night()
        self._radarr_l()
        self.listing(stream(LABEL, 188327), stream(LABEL, 555)); self.night()
        self.assertEqual("\x1f" + LABEL + "\tmp4", self.override().label)
        self.listing(stream(LABEL, 188327)); self.night()           # twin gone: stays shared
        self.assertTrue(self.override().label.startswith("\x1f"))
        self.listing(stream("Renamed (1981)", 188327)); self.night()  # real rename clears
        self.assertEqual("Renamed (1981)\tmp4", self.override().label)

    def test_undo_on_a_followed_fix_later_marked_shared(self):
        self.listing(stream(LABEL, 188327)); self.night(); self.fix(); self.night()
        self.listing(stream(LABEL, 999001)); self.night()
        self.assertEqual("999001", self.override().stream_key)
        self._radarr_l()
        self.listing(stream(LABEL, 999001), stream(LABEL, 555)); self.night()
        self.assertTrue(self.override().label.startswith("\x1f"))
        r = wrong_match.undo_followed_fix(self.db, 674607, user_name="admin")
        self.assertTrue(r["ok"])
        self.db.expire_all()
        self.assertTrue(self.strm(674607).read_text().strip().endswith("/188327.mp4"))

    def test_block_shared_marker(self):
        self.listing(stream(LABEL, 188327)); self.night(); self.block(); self.night()
        self.listing(stream(LABEL, 188327), stream(LABEL, 555)); self.night()
        b = self.db.query(BlockedStream).one()
        self.assertTrue(b.label.startswith("\x1f"))


class TestRuntimeCheckKeepsTheFlag(_Fixed):
    def test_a_relist_flag_stays_while_its_film_is_here(self):
        self.listing(stream("Filler 1 (2010)", 5001))
        self.night()
        self.db.add(MatchSuspect(tmdb_id=5001, media_type="movie", title="x", reason="relist_followed"))
        self.db.add(MatchSuspect(tmdb_id=5002, media_type="movie", title="y"))
        self.db.add(MatchSuspect(tmdb_id=999999, media_type="movie", title="gone", reason="relist_blocked"))
        self.db.commit()
        fake = mock.Mock()
        fake.query_movies_with_media_sources.return_value = []
        with mock.patch.object(wrong_match, "get_setting", lambda db, k, d="": "x"), \
                mock.patch("services.jellyfin.JellyfinService", return_value=fake):
            wrong_match.check_runtime_mismatches(self.db)
        self.assertEqual([5001], [s.tmdb_id for s in self.db.query(MatchSuspect)])


class TestProperty(_Fixed):
    """Random nights: re-listings of the fixed and the blocked stream (same
    label or not), both ids listed, duplicate labels, an unread category."""

    def test_random_nights(self):
        import logging
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        bad = []
        base_setup = self.setUp
        for seed in range(SEEDS):
            rnd = random.Random(seed)
            if seed:
                self.doCleanups()
                self.tearDown()
                base_setup()
            TMDB.films[3000] = ("Other Label Film", "1990")
            TMDB.search["Other Label Film"] = 3000
            fixed_key, blocked_key = 188327, 177000
            self.listing(stream(LABEL, fixed_key), stream("Other Label Film (1990)", blocked_key))
            self.night()
            self.fix()
            wrong_match.block_and_remove_movie(self.db, 3000, user_name="admin")
            self.db.expire_all()
            path = self.strm(674607)
            if rnd.random() < 0.3:
                # The genuine label film belongs to another source (a download):
                # its provider stream is then no film of ours (N1-D2)
                self.db.add(Movie(tmdb_id=21137, title="The Decline of Western Civilization", year="1981",
                                  source="radarr", radarr_path="/media/movies/D/D.mkv"))
                self.db.commit()
            coexisted = set()  # streams listed with the label next to the fixed stream
            learning_twin = rnd.random() < 0.25
            if learning_twin:
                # A twin on the very night the labels are first stored (N1-D2b)
                self.listing(stream(LABEL, fixed_key), stream(LABEL, 555),
                             stream("Other Label Film (1990)", blocked_key))
                coexisted.add("555")
            self.night()
            cur_fixed, cur_blocked = fixed_key, blocked_key
            spoiled = learning_twin   # the label is shared: a later re-listing is placed as usual
            for night in range(3):
                if rnd.random() < 0.45:
                    cur_fixed = 900000 + night * 10 + rnd.randint(0, 5)
                if rnd.random() < 0.3:
                    cur_blocked = 800000 + night * 10 + rnd.randint(0, 5)
                streams = [stream(LABEL, cur_fixed)]
                if rnd.random() < 0.15:
                    streams.append(stream(LABEL, cur_fixed + 1))      # a second stream with the label
                ov_key = self.db.query(MatchOverride).one().stream_key
                if rnd.random() < 0.15 and str(cur_fixed) != ov_key:
                    streams.append(stream(LABEL, int(ov_key)))        # the old id is still listed
                if rnd.random() < 0.2:
                    streams.append(stream(LABEL, 555))                 # a twin, next to the fixed stream
                if coexisted and rnd.random() < 0.5:
                    # The fixed upload is simply dropped; only the twin is left
                    streams = [stream(LABEL, 555)]
                streams.append(stream("Other Label Film (1990)", cur_blocked))
                self.listing(*streams)
                unread = rnd.random() < 0.15
                fault = rnd.random() < 0.15                              # TMDB errors for both labels tonight
                if unread:
                    self.client.movies["b"] = [stream("Heat (1995)", 900)]
                    self.client.raise_for = {"b"}
                else:
                    self.client.movies.pop("b", None)
                    self.client.raise_for = set()
                real = YearStrictTMDB.search_movie

                def search(self_, name, year=None, _real=real, _fault=fault, **k):
                    if _fault and name in ("The Decline of Western Civilization", "Other Label Film"):
                        raise RuntimeError("TMDB 503")
                    return _real(self_, name, year, **k)
                with mock.patch.object(YearStrictTMDB, "search_movie", search):
                    sync.sync_provider(self.p, "full", self.db)
                sync.sweep_orphaned_vod_records(self.db)
                self.db.expire_all()
                listed = {str(x["stream_id"]) for x in streams}
                if ov_key in listed:
                    coexisted |= {str(x["stream_id"]) for x in streams if x["name"] == LABEL} - {ov_key}
                labelled = sum(1 for x in streams if x["name"] == LABEL)
                ctx = f"seed {seed} night {night} (listing {sorted(listed)}, override was {ov_key}, unread={unread})"
                ov = self.db.query(MatchOverride).one()
                # P1: the override never moves while its key is listed or on an unread night
                if ov.stream_key != ov_key and (unread or ov_key in listed):
                    bad.append(f"{ctx}: the fix moved to {ov.stream_key}")
                if labelled != 1 or fault:
                    # Two streams with the label: not a re-listing the sync can
                    # tell apart; they are placed by their label, as before
                    spoiled = True
                # P2: a clean re-listing keeps the fixed copy, in place, on the new stream; no label film
                if not spoiled and not unread and ov_key not in listed and labelled == 1:
                    row = self.db.query(Movie).filter_by(tmdb_id=674607).first()
                    if row is None or _RealPath(row.strm_path) != path:
                        bad.append(f"{ctx}: the fixed copy is gone or moved")
                    elif not path.read_text().endswith(f"/{cur_fixed}.mp4"):
                        bad.append(f"{ctx}: the fixed copy plays {path.read_text()[-14:]}")
                    if ov.stream_key != str(cur_fixed):
                        bad.append(f"{ctx}: the fix did not follow ({ov.stream_key})")
                label_film = self.db.query(Movie).filter(Movie.tmdb_id == 21137,
                                                         Movie.source.like("provider_%")).first()
                if not spoiled and label_film is not None:
                    bad.append(f"{ctx}: the label's film was imported")
                # P5 (N1-D2): the fixed copy never plays a stream listed next to the fixed one
                fixed_row = self.db.query(Movie).filter_by(tmdb_id=674607).first()
                if fixed_row is not None and fixed_row.strm_path:
                    now_plays = _RealPath(fixed_row.strm_path).read_text().rsplit("/", 1)[-1].split(".")[0]
                    if now_plays in coexisted:
                        bad.append(f"{ctx}: the fixed copy plays {now_plays}, listed next to its own stream")
                # P3: never two rows playing one stream
                plays = [_RealPath(m.strm_path).read_text() for m in self.db.query(Movie) if m.strm_path]
                if len(plays) != len(set(plays)):
                    bad.append(f"{ctx}: two rows play one stream")
                # P4: a blocked film that came back under a new id is flagged for
                # the admin, TMDB errors or not (N1-D1)
                if self.db.query(Movie).filter_by(tmdb_id=3000).first() is not None:
                    fl = self.db.query(MatchSuspect).filter_by(tmdb_id=3000).first()
                    if fl is None or fl.reason != "relist_blocked":
                        bad.append(f"{ctx}: a re-listed blocked film came back without a flag")
        print(f"\nN1 property: {SEEDS} seeds, {len(bad)} violations")
        for b in bad[:6]:
            print("  ", b)
        self.assertEqual([], bad)


if __name__ == "__main__":
    unittest.main()
