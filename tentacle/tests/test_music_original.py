"""The original-release rule (services/music/original.py).

Required cases: a genuine double album that's right, a 4-disc deluxe pinned
wrongly, a vinyl-only original, a hidden-track tie, no dated release. Plus the
four refinements agreed after checking the curator report against
MusicBrainz: most common disc count (not fewest), physical releases before
digital ones, ties go to review, and hidden-track editions go to review
instead of being trimmed. The shapes are taken from real albums.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import itertools
import unittest

from services.music.original import (
    REPIN, REPIN_DOWNLOAD, REPIN_TRIM, REVIEW, RIGHT, Ambiguous, Original, Prefs, choose_for_request,
    choose_lidarr_release, country_rank, edition_marked, evaluate, find_original, representative_release,
)

_ids = itertools.count(1)


def mb(date, tracks, fmt="CD", discs=1, country="US", title="Album", status="Official", disambiguation=""):
    """A MusicBrainz release: `tracks` split over `discs` media of format `fmt`."""
    per, extra = divmod(tracks, discs)
    media = [{"format": fmt, "track-count": per + (1 if i < extra else 0)} for i in range(discs)]
    return {"id": f"mb-{next(_ids)}", "date": date, "status": status, "country": country,
            "title": title, "disambiguation": disambiguation, "media": media}


def lr(tracks, discs=1, fmt="CD", monitored=False, title="Album", disambiguation="", country=None,
       status="Official", release_id=None):
    """A Lidarr release."""
    return {"id": next(_ids), "foreignReleaseId": release_id or f"lr-{next(_ids)}", "title": title,
            "disambiguation": disambiguation, "trackCount": tracks, "mediumCount": discs,
            "format": fmt, "country": country or ["United States"], "status": status, "monitored": monitored}


def album(releases, have=0, title="Album", any_release_ok=True):
    return {"title": title, "releases": releases, "anyReleaseOk": any_release_ok,
            "statistics": {"trackFileCount": have}}


P = Prefs()


class TestRequiredCases(unittest.TestCase):
    def test_a_genuine_double_album_is_right(self):
        # The Wall, 1979: ten double-LP releases and two cassettes MusicBrainz lists as one medium.
        rels = [mb("1979-11-30", 26, '12" Vinyl', discs=2) for _ in range(10)] + \
               [mb("1979-12-01", 26, "Cassette", discs=1) for _ in range(2)] + \
               [mb("2011-09-26", 26, "Digital Media")]
        pinned = lr(26, discs=2, fmt='2x12" Vinyl', monitored=True)
        v = evaluate(album([pinned, lr(26, 1, "Digital Media")], have=26), rels, P)
        self.assertEqual(v.category, RIGHT, v)
        self.assertEqual(v.original, {"year": "1979", "tracks": 26, "discs": 2})

    def test_a_four_disc_deluxe_pinned_wrongly(self):
        # Wish You Were Here: a 1975 five-track LP; Lidarr pinned the 4-disc Immersion box.
        rels = [mb("1975-09-12", 5, '12" Vinyl') for _ in range(8)] + [mb("2011-11-07", 32, "CD", discs=4)]
        box = lr(32, discs=4, monitored=True, title="Wish You Were Here", disambiguation="Immersion box set")
        cd = lr(5, discs=1, fmt="CD", title="Wish You Were Here")
        v = evaluate(album([box, cd], have=0, title="Wish You Were Here"), rels, P)
        self.assertEqual(v.category, REPIN_DOWNLOAD)
        self.assertEqual(v.target["id"], cd["foreignReleaseId"])
        # Downloaded already: the 27 extra tracks go.
        self.assertEqual(evaluate(album([box, cd], have=32), rels, P).category, REPIN_TRIM)

    def test_a_vinyl_only_original(self):
        # Living the Blues, 1968: only ever a double LP; the tracklist is what matters.
        rels = [mb("1968-11-01", 10, '12" Vinyl', discs=2) for _ in range(3)] + [mb("1994-01-01", 10, "CD")]
        vinyl = lr(10, discs=2, fmt='2x12" Vinyl')
        cd = lr(10, discs=1, fmt="CD", monitored=True)
        v = evaluate(album([vinyl, cd], have=10), rels, P)
        self.assertEqual(v.category, REPIN)  # same files, pinned to the double LP
        self.assertEqual(v.target["id"], vinyl["foreignReleaseId"])
        # Only the CD in Lidarr: its tracklist is the original, so it's right.
        self.assertEqual(evaluate(album([cd], have=10), rels, P).category, RIGHT)

    def test_a_hidden_track_tie_goes_to_review_with_the_options(self):
        # The Stranger, 1977: three releases with 9 tracks, three with 10.
        rels = [mb("1977-09-29", 9, '12" Vinyl') for _ in range(3)] + \
               [mb("1977-10-01", 10, '12" Vinyl') for _ in range(3)]
        nine, ten = lr(9), lr(10, monitored=True)
        v = evaluate(album([nine, ten], have=9), rels, P)
        self.assertEqual((v.category, v.reason), (REVIEW, "tie"))
        self.assertEqual([o["tracks"] for o in v.options], [9, 10])
        self.assertEqual([o["target"]["id"] for o in v.options], [nine["foreignReleaseId"], ten["foreignReleaseId"]])
        self.assertIn("equally common", v.message)

    def test_no_dated_official_release(self):
        rels = [mb("", 11), mb("1990-01-01", 11, status="Bootleg"), mb("2001-01-01", 0)]
        eleven, fourteen = lr(11, monitored=True), lr(14)
        v = evaluate(album([eleven, fourteen], have=11), rels, P)
        self.assertEqual((v.category, v.reason), (REVIEW, "no_dated_release"))
        # Nothing from MusicBrainz to choose between: Lidarr's releases are the options.
        self.assertEqual([(o["tracks"], o["target"]["id"]) for o in v.options],
                         [(11, eleven["foreignReleaseId"]), (14, fourteen["foreignReleaseId"])])


class TestAgreedRefinements(unittest.TestCase):
    def test_disc_count_is_the_most_common_not_the_fewest(self):
        # Stadium Arcadium, 2006: eight double CDs, two 4-LP sets, one cassette.
        rels = [mb("2006-05-05", 28, "CD", discs=2) for _ in range(8)] + \
               [mb("2006-05-09", 28, '12" Vinyl', discs=4) for _ in range(2)] + [mb("2006-05-09", 28, "Cassette")]
        self.assertEqual(find_original(rels).discs, 2)
        v = evaluate(album([lr(28, 2, "2xCD", monitored=True), lr(28, 1, "Digital Media")], have=28), rels, P)
        self.assertEqual(v.category, RIGHT)

    def test_a_misdated_digital_release_does_not_count(self):
        # Radio City, 1974: the LP had 12 tracks; a "1974" digital release has 13.
        rels = [mb("1974-01-01", 12, '12" Vinyl'), mb("1974-01-01", 13, "Digital Media")]
        found = find_original(rels)
        self.assertIsInstance(found, Original)
        self.assertEqual(found.tracks, 12)
        self.assertEqual(evaluate(album([lr(12, monitored=True), lr(13)], have=12), rels, P).category, RIGHT)

    def test_a_digital_only_first_year_still_counts(self):
        rels = [mb("2020-04-14", 10, "Digital Media") for _ in range(2)] + [mb("2021-01-01", 12, "CD")]
        found = find_original(rels)
        self.assertEqual((found.tracks, found.digital_only_year), (10, True))

    def test_a_hidden_track_edition_matching_your_files_goes_to_review(self):
        # Mellow Gold, 1994: eight 12-track releases, three 13-track pressings.
        rels = [mb("1994-03-01", 12) for _ in range(8)] + [mb("1994-03-01", 13) for _ in range(3)]
        twelve, thirteen = lr(12), lr(13, monitored=True)
        v = evaluate(album([twelve, thirteen], have=13), rels, P)
        self.assertEqual((v.category, v.reason), (REVIEW, "hidden_track"), v)
        self.assertEqual([o["tracks"] for o in v.options], [12, 13])
        # Pinned to the 12-track original: simply right.
        self.assertEqual(evaluate(album([dict(twelve, monitored=True), dict(thirteen, monitored=False)], have=12),
                                  rels, P).category, RIGHT)
        # Nothing downloaded yet: nothing to lose, so pin the original.
        self.assertEqual(evaluate(album([twelve, thirteen], have=0), rels, P).category, REPIN_DOWNLOAD)

    def test_a_clear_majority_extra_track_is_trimmed_when_not_one_track_away(self):
        # Vitalogy: 14 tracks originally; a 17-track edition pinned with 17 files.
        rels = [mb("1994-12-06", 14) for _ in range(5)] + [mb("1994-12-06", 17)]
        v = evaluate(album([lr(14), lr(17, monitored=True)], have=17), rels, P)
        self.assertEqual(v.category, REPIN_TRIM)


class TestChoosingTheRelease(unittest.TestCase):
    def setUp(self):
        self.orig = Original(year="1970", tracks=11, discs=1, counts={11: 3}, near=[], releases=[])

    def test_official_then_edition_words_then_discs_then_format_then_country(self):
        order = [
            lr(11, 1, "CD", country=["United States"]),                        # best
            lr(11, 1, "CD", country=["Japan"]),                                # country
            lr(11, 1, '12" Vinyl', country=["United States"]),                 # format
            lr(11, 2, "2xCD", country=["United States"]),                      # discs
            lr(11, 1, "CD", disambiguation="Deluxe Edition"),                  # edition word
            lr(11, 1, "CD", status="Promotion"),                               # not official
        ]
        remaining = list(order)
        for expected in order:
            got = choose_lidarr_release(self.orig, remaining, P)
            self.assertIs(got, expected)
            remaining.remove(got)

    def test_only_the_exact_track_count_is_pinned(self):
        self.assertIsNone(choose_lidarr_release(self.orig, [lr(10), lr(12)], P))

    def test_edition_words_that_are_the_album_title_do_not_count(self):
        self.assertFalse(edition_marked("Live at Leeds", P.edition_words, "Live at Leeds"))
        self.assertTrue(edition_marked("Live at Leeds (Deluxe Edition)", P.edition_words, "Live at Leeds"))
        self.assertFalse(edition_marked("Boxer", ["box"]))  # whole words only

    def test_countries_by_code_or_name(self):
        self.assertEqual(country_rank(["United Kingdom"], ["US", "GB"]), 1)
        self.assertEqual(country_rank(["GB"], ["US", "GB"]), 1)
        self.assertEqual(country_rank(["[Worldwide]"], ["US", "XW"]), 1)
        self.assertEqual(country_rank(["Japan"], ["US", "GB"]), 2)

    def test_musicbrainz_data_decides_country_and_format_when_lidarr_lacks_it(self):
        us = mb("1970-01-01", 11, "CD", country="US")
        jp = mb("1970-01-01", 11, "CD", country="JP")
        a = lr(11, release_id=jp["id"], country=["?"])
        b = lr(11, release_id=us["id"], country=["?"])
        self.assertIs(choose_lidarr_release(self.orig, [a, b], P, {r["id"]: r for r in (us, jp)}), b)

    def test_no_release_of_the_right_length_goes_to_review(self):
        rels = [mb("1970-01-01", 11) for _ in range(3)]
        v = evaluate(album([lr(13, monitored=True)], have=13), rels, P)
        self.assertEqual((v.category, v.reason), (REVIEW, "no_matching_release"))
        self.assertIn("13", v.message)
        self.assertEqual([o["tracks"] for o in v.options], [13])

    def test_right_records_whether_the_pin_is_locked(self):
        rels = [mb("1970-01-01", 11) for _ in range(3)]
        self.assertFalse(evaluate(album([lr(11, monitored=True)], have=11), rels, P).locked)
        self.assertTrue(evaluate(album([lr(11, monitored=True)], have=11, any_release_ok=False), rels, P).locked)

    def test_a_right_tracklist_on_another_disc_count_is_repinned_only_if_a_better_one_exists(self):
        rels = [mb("1985-05-13", 9) for _ in range(5)]
        two_lp = lr(9, 2, '2x12" Vinyl', monitored=True)
        self.assertEqual(evaluate(album([two_lp, lr(9, 1, "CD")], have=9), rels, P).category, REPIN)
        self.assertEqual(evaluate(album([two_lp], have=9), rels, P).category, RIGHT)


class TestRequests(unittest.TestCase):
    def test_a_clear_original_is_pinned(self):
        rels = [mb("1970-01-01", 11) for _ in range(3)]
        cd = lr(11)
        self.assertIs(choose_for_request(album([lr(15), cd]), rels, P), cd)

    def test_an_ambiguous_album_waits_for_a_choice(self):
        rels = [mb("1977-09-29", 9) for _ in range(3)] + [mb("1977-09-29", 10) for _ in range(3)]
        nine, ten = lr(9), lr(10)
        out = choose_for_request(album([nine, ten]), rels, P)
        self.assertIsInstance(out, Ambiguous)
        self.assertEqual(out.reason, "tie")
        self.assertIs(choose_for_request(album([nine, ten]), rels, P, {"tracks": 10}), ten)
        self.assertIs(choose_for_request(album([nine, ten]), rels, P, {"release_id": nine["foreignReleaseId"]}), nine)

    def test_representative_release_for_the_album_page(self):
        rels = [mb("1970-01-01", 11, "CD", country="JP"), mb("1970-02-01", 11, "CD", country="US"),
                mb("1970-01-01", 11, '12" Vinyl', country="US")]
        found = find_original(rels)
        self.assertIs(representative_release(found, P), rels[1])


if __name__ == "__main__":
    unittest.main()
