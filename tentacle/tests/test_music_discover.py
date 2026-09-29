"""Music module, phase 4: Discover → Music and Spotify playlist imports.

Chart sources and MusicBrainz are faked; the rules under test are how chart
entries become requestable albums (title cleanup, artist credits, studio albums
only), how the sections are built and kept fresh, and how a playlist becomes a
preview of albums that are requested one at a time through the single path.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import io
import json
import time
import unittest
from datetime import date, timedelta
from unittest import mock

from web_stubs import _ensure_web_stubs

_ensure_web_stubs()

from test_music_core import _Base  # noqa: E402

A1, A2, A3 = ("11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222",
              "33333333-3333-3333-3333-333333333333")
R1, R2, R3, R4 = ("aaaaaaaa-0000-0000-0000-000000000001", "aaaaaaaa-0000-0000-0000-000000000002",
                  "aaaaaaaa-0000-0000-0000-000000000003", "aaaaaaaa-0000-0000-0000-000000000004")


def rg(rgid, title, artist, artist_id, kind="Album", secondary=(), year="1977", genres=(), score=100):
    return {"id": rgid, "title": title, "primary-type": kind, "secondary-types": list(secondary),
            "first-release-date": f"{year}-01-01", "score": score,
            "artist-credit": [{"name": artist, "artist": {"id": artist_id, "name": artist}}],
            "genres": [{"name": n, "count": c} for n, c in genres]}


class FakeMB:
    def __init__(self):
        self.artists, self.groups, self.rgs, self.lists = {}, {}, {}, {}

    def search_artists(self, q, limit=8):
        return self.artists.get(q, [])

    def find_release_groups(self, title, artist, limit=10):
        return self.groups.get((title, artist), [])

    def release_group(self, rgid):
        value = self.rgs[rgid]
        if isinstance(value, Exception):
            raise value
        return value

    def find_release_groups_batch(self, items):
        self.searches = getattr(self, "searches", 0) + 1
        out = []
        for title, artists in items:
            for a in artists:
                out += self.groups.get((title, a), [])
        return out

    def recordings_batch(self, items):
        self.recording_searches = getattr(self, "recording_searches", 0) + 1
        out = []
        for title, ids in items:
            for i in ids:
                out += getattr(self, "recordings", {}).get((title, i), [])
        return out[:100]

    def release_groups_by_id(self, rgids):
        self.batches = getattr(self, "batches", 0) + 1
        return {i: self.rgs[i] for i in rgids if isinstance(self.rgs.get(i), dict)}

    def series(self, sid):
        value = self.lists[sid]
        if isinstance(value, Exception):
            raise value
        return value


def artist_hit(name, mbid, score=100):
    return {"id": mbid, "name": name, "score": score}


class TestTitlesAndCredits(unittest.TestCase):
    def test_extras_are_dropped_but_a_titles_own_words_stay(self):
        from services.music.discover import clean_title
        cases = {"Dreams - 2004 Remaster": "Dreams", "Hey Jude - Remastered 2015": "Hey Jude",
                 "Money - 2011 Remastered Version": "Money", "Rap Killa (feat. Azaad 4L)": "Rap Killa",
                 "Last Thing You Need (from GTAVI: The Album)": "Last Thing You Need",
                 "Gimme Shelter (Live)": "Gimme Shelter", "Song - Radio Edit": "Song",
                 "(Don't Fear) The Reaper": "(Don't Fear) The Reaper", "Live and Let Die": "Live and Let Die",
                 "Sweet Dreams (Are Made of This)": "Sweet Dreams (Are Made of This)",
                 "Midnights - EP": "Midnights", "Rumours (Super Deluxe)": "Rumours"}
        for raw, want in cases.items():
            self.assertEqual(clean_title(raw), want, raw)

    def test_a_credit_is_tried_whole_before_its_first_name(self):
        from services.music.discover import artist_candidates
        self.assertEqual(artist_candidates("Crosby, Stills, Nash & Young"), ["Crosby, Stills, Nash & Young", "Crosby"])
        self.assertEqual(artist_candidates("Karan Aujla & MXRCI"), ["Karan Aujla & MXRCI", "Karan Aujla"])
        self.assertEqual(artist_candidates("Taylor Swift"), ["Taylor Swift"])
        self.assertEqual(artist_candidates("Lil Baby feat. Future"), ["Lil Baby feat. Future", "Lil Baby"])
        self.assertEqual(artist_candidates("AC/DC, Someone")[-1], "AC/DC")   # a slash is part of a name
        self.assertEqual(artist_candidates("HUNTR/X, EJAE, AUDREY NUNA")[-1], "HUNTR/X")
        self.assertEqual(artist_candidates("Skrillex x Diplo")[-1], "Skrillex")

    def test_genre_tags_become_discover_genres(self):
        from services.music.discover import families_of
        ok_computer = [{"name": "alternative rock", "count": 25}, {"name": "art rock", "count": 13},
                       {"name": "rock", "count": 13}, {"name": "experimental", "count": 3}]
        self.assertEqual(families_of(ok_computer), ["Rock"])
        self.assertEqual(families_of([{"name": "pop rock", "count": 4}, {"name": "synth-pop", "count": 3}]),
                         ["Rock", "Pop"])
        # Whole words: "dub" is Reggae, "dubstep" is Electronic; weak tags don't count.
        self.assertEqual(families_of([{"name": "dubstep", "count": 6}, {"name": "dub", "count": 1}]), ["Electronic"])
        self.assertEqual(families_of([{"name": "experimental", "count": 3}]), [])
        self.assertEqual(families_of([]), [])
        # Hybrid Theory is tagged nu metal, rap metal, rap rock: metal and rock, not hip hop.
        self.assertEqual(families_of([{"name": "nu metal", "count": 9}, {"name": "rap metal", "count": 8},
                                      {"name": "rap rock", "count": 6}]), ["Metal", "Rock"])

    def test_a_genre_names_last_word_is_what_it_is(self):
        from services.music.discover import family_of
        cases = {"rap metal": "Metal", "funk metal": "Metal", "baroque pop": "Pop", "rock opera": "Rock",
                 "blues rock": "Rock", "post-punk revival": "Rock", "punk rock": "Punk", "pop punk": "Punk",
                 "west coast hip hop": "Hip hop", "hip-hop": "Hip hop", "jazz rap": "Hip hop",
                 "alternative r&b": "R&B and soul", "rhythm and blues": "R&B and soul", "indie folk": "Folk",
                 "alt-country": "Country", "trip hop": "Electronic", "smooth jazz": "Jazz",
                 "modern classical": "Classical", "experimental": None}
        for genre, want in cases.items():
            self.assertEqual(family_of(genre), want, genre)


class TestWhatCounts(unittest.TestCase):
    def test_chart_entries_that_are_not_records(self):
        from services.music.discover import is_record
        self.assertFalse(is_record({"title": "I'm So Happy", "genres": ["Children’s Music"]}))
        self.assertFalse(is_record({"title": "KPop Demon Hunters (Soundtrack from the Netflix Film)",
                                    "genres": ["K-Pop", "Soundtrack"]}))
        self.assertFalse(is_record({"title": "Brown Noise: Loops for Relaxation, Deep Sleep", "genres": []}))
        self.assertTrue(is_record({"title": "Sleep Well Beast", "genres": ["Alternative"]}))
        self.assertTrue(is_record({"title": "Lullaby", "genres": ["Rock"]}))

    def test_a_various_artists_album_is_not_where_a_song_came_out(self):
        # Real case: "Tennessee Whiskey" is on a 2012 charity album (Various Artists)
        # before Chris Stapleton's Traveller (2015).
        from services.music.browse import original_album_for_song

        def release(rgid, title, date, artist_id):
            return {"status": "Official", "date": date, "artist-credit": [{"artist": {"id": artist_id}}],
                    "release-group": {"id": rgid, "title": title, "primary-type": "Album", "secondary-types": []}}

        class MB:
            def artist(self, mbid):
                return {"relations": []}

            def recordings_by(self, title, ids, limit=100, albums_only=False):
                return [{"id": "r1", "title": "Tennessee Whiskey",
                         "releases": [release(R1, "Life Goes On", "2012", "various"),
                                      release(R2, "Traveller", "2015", A1)]}]
        self.assertEqual(original_album_for_song(MB(), "Tennessee Whiskey", A1)["album"]["id"], R2)


class TestSongRule(unittest.TestCase):
    """Where a song came out: fixes found importing Spotify's "Rock Classics"."""

    class MB:
        def __init__(self, relations=(), recordings=None):
            self.relations, self.recordings, self.queries = list(relations), recordings or {}, []

        def artist(self, mbid):
            return {"relations": self.relations}

        def recordings_by(self, title, ids, limit=100, albums_only=False):
            self.queries.append((tuple(ids), albums_only))
            return self.recordings.get(albums_only, [])

    @staticmethod
    def recording(rgid, album, date, artist_id, kind="Album", secondary=()):
        return {"id": "r", "title": "Song", "releases": [{
            "status": "Official", "date": date, "artist-credit": [{"artist": {"id": artist_id}}],
            "release-group": {"id": rgid, "title": album, "primary-type": kind, "secondary-types": list(secondary)}}]}

    def test_a_bands_members_are_not_the_band(self):
        from services.music.browse import related_artists
        mb = self.MB(relations=[{"type": "member of band", "direction": "backward", "artist": {"id": A2}},
                                {"type": "member of band", "direction": "forward", "artist": {"id": A3}}])
        self.assertEqual(related_artists(mb, A1), [A1, A3])   # the band A1 belongs to; not A1's member A2

    def test_the_artists_own_soundtrack_album_counts(self):
        from services.music.browse import original_album_for_song
        mb = self.MB(recordings={True: [self.recording(R1, "Purple Rain", "1984", A1, secondary=["Soundtrack"]),
                                        self.recording(R2, "Sign o' the Times", "1987", A1)]})
        self.assertEqual(original_album_for_song(mb, "Song", A1)["album"]["id"], R1)

    def test_album_recordings_first_all_recordings_only_to_explain_a_miss(self):
        from services.music.browse import original_album_for_song
        mb = self.MB(recordings={True: [self.recording(R1, "Aftermath", "1966", A1)]})
        self.assertEqual(original_album_for_song(mb, "Song", A1)["album"]["id"], R1)
        self.assertEqual(mb.queries, [((A1,), True)])          # one search, albums only
        single = self.MB(recordings={False: [self.recording(R2, "Song", "1965", A1, kind="Single")]})
        found = original_album_for_song(single, "Song", A1)
        self.assertIsNone(found["album"])
        self.assertEqual([q[1] for q in single.queries], [True, False])
        self.assertEqual(found["singles"][0]["id"], R2)          # "only on singles"


class TestMatching(unittest.TestCase):
    def setUp(self):
        self.mb = FakeMB()

    def test_an_exact_artist_name_is_required(self):
        from services.music.discover import resolve_artist
        self.mb.artists["The Beatles"] = [artist_hit("The Beatles Revival", A2, 100), artist_hit("The Beatles", A1, 98)]
        self.assertEqual(resolve_artist(self.mb, "The Beatles")["mbid"], A1)
        self.mb.artists["Beatles"] = [artist_hit("The Beatles", A1)]
        self.assertEqual(resolve_artist(self.mb, "Beatles")["mbid"], A1)   # a leading "The" doesn't matter
        self.mb.artists["Future"] = [artist_hit("Future Islands", A3)]
        self.assertIsNone(resolve_artist(self.mb, "Future"))

    def test_a_chart_album_resolves_to_the_studio_album_only(self):
        from services.music.discover import find_album
        self.mb.groups[("Rumours", "Fleetwood Mac")] = [
            rg(R2, "Rumours", "Fleetwood Mac", A1, kind="Single", score=100),
            rg(R1, "Rumours", "Fleetwood Mac", A1, score=90)]
        self.assertEqual(find_album(self.mb, "Rumours (Super Deluxe)", "Fleetwood Mac")["id"], R1)
        self.mb.groups[("Greatest Hits", "Journey")] = [rg(R3, "Greatest Hits", "Journey", A2, secondary=["Compilation"])]
        self.assertIsNone(find_album(self.mb, "Greatest Hits (2024 Remaster)", "Journey"))
        self.assertIsNone(find_album(self.mb, "Unknown", "Nobody"))

    def test_a_song_resolves_like_song_search_with_fallbacks(self):
        from services.music import discover
        self.mb.artists["Karan Aujla"] = [artist_hit("Karan Aujla", A1)]
        self.mb.artists["Phil Collins"] = [artist_hit("Phil Collins", A2)]
        # The recording's dates can be a remaster's; the year shown is the album's own.
        albums = {("Rap Killa", A1): {"id": R1, "title": "P-POP", "date": "2026-08-01"},
                  ("Against All Odds (Take a Look at Me Now)", A2): {"id": R2, "title": "Against All Odds",
                                                                    "date": "2016-03-01"}}
        self.mb.rgs[R1] = rg(R1, "P-POP", "Karan Aujla", A1, year="2026")
        self.mb.rgs[R2] = rg(R2, "Against All Odds", "Phil Collins", A2, year="1984")

        def fake_original(mb, title, artist_mbid):
            album = albums.get((title, artist_mbid))
            return {"album": album, "recordings": [], "singles": [] if album else [{"id": "x"}]}
        with mock.patch.object(discover, "original_album_for_song", side_effect=fake_original):
            # "A & B" credit: the whole credit isn't an artist, the first name is.
            got = discover.resolve_song(self.mb, "Rap Killa (feat. Azaad 4L)", "Karan Aujla & MXRCI")
            self.assertEqual((got["album"]["mbid"], got["album"]["year"]), (R1, "2026"))
            # A trailing part that is really the title: retried as written.
            got = discover.resolve_song(self.mb, "Against All Odds (Take a Look at Me Now)", "Phil Collins")
            self.assertEqual((got["album"]["mbid"], got["album"]["year"]), (R2, "1984"))
            got = discover.resolve_song(self.mb, "B-side", "Phil Collins")
            self.assertIn("singles", got["reason"])
            self.assertIn("no artist", discover.resolve_song(self.mb, "X", "Nobody")["reason"])
            # A Spotify export's album name is the last resort.
            self.mb.groups[("Face Value", "Phil Collins")] = [rg(R3, "Face Value", "Phil Collins", A2)]
            got = discover.resolve_song(self.mb, "Hidden Gem", "Phil Collins", album="Face Value")
            self.assertEqual(got["album"]["mbid"], R3)


def lidarr_album(rgid, title, artist, artist_id, kind="Album", secondary=(), date="1977-02-04"):
    return {"foreignAlbumId": rgid, "title": title, "albumType": kind, "secondaryTypes": list(secondary),
            "releaseDate": f"{date}T00:00:00Z", "images": [{"coverType": "cover", "remoteUrl": f"https://img/{rgid}"}],
            "artist": {"artistName": artist, "foreignArtistId": artist_id}}


class FakeLidarrLookups:
    def __init__(self):
        self.albums, self.artists, self.calls, self.fail = {}, {}, [], False

    def lookup_albums(self, term):
        from services.lidarr import LidarrError
        self.calls.append(term)
        if self.fail:
            raise LidarrError("Lidarr is down")
        if term in getattr(self, "unanswerable", ()):
            raise LidarrError("Lidarr answered HTTP 503", 503)
        return self.albums.get(term, [])

    def lookup_artists(self, term):
        self.calls.append(term)
        return self.artists.get(term, [])


class TestResolver(unittest.TestCase):
    """Lidarr's metadata server and Deezer first; MusicBrainz for what they can't tell."""

    def setUp(self):
        from services.music import discover
        self.discover = discover
        self.mb = FakeMB()
        self.r = discover.Resolver.__new__(discover.Resolver)
        self.r.mb, self.r.lidarr, self.r.deezer = self.mb, FakeLidarrLookups(), True
        self.r.reset()

    def test_albums_come_from_lidarr_studio_and_earliest(self):
        self.r.lidarr.albums["Fleetwood Mac Rumours"] = [
            lidarr_album(R2, "Fleetwood Mac / Rumours", "Fleetwood Mac", A1, secondary=["Compilation"]),
            lidarr_album(R3, "Rumours", "Fleetwood Mac", A1, secondary=["Live"], date="2019-08-09"),
            lidarr_album(R1, "Rumours", "Fleetwood Mac", A1),
            lidarr_album(R4, "Rumours", "Tribute Band", A2)]
        card = self.r.album("Rumours (Super Deluxe)", "Fleetwood Mac")
        self.assertEqual((card["mbid"], card["year"], card["cover"]), (R1, "1977", f"https://img/{R1}"))
        # Lidarr knows the title but only as a live album: no MusicBrainz, no album.
        self.r.lidarr.albums["Nirvana MTV Unplugged In New York"] = [
            lidarr_album(R2, "MTV Unplugged In New York", "Nirvana", A2, secondary=["Live"])]
        self.assertIsNone(self.r.album("MTV Unplugged In New York", "Nirvana"))
        # Lidarr doesn't know it: MusicBrainz decides.
        self.mb.groups[("Brand New", "Someone")] = [rg(R4, "Brand New", "Someone", A3, year="2026")]
        self.assertEqual(self.r.album("Brand New", "Someone")["mbid"], R4)
        self.assertIsNone(self.r.album("Brand New", "Someone", fallback=False) and None)

    def test_when_lidarr_fails_musicbrainz_takes_over(self):
        self.r._failures = 0
        lidarr = self.r.lidarr
        lidarr.fail = True
        self.mb.groups[("Rumours", "Fleetwood Mac")] = [rg(R1, "Rumours", "Fleetwood Mac", A1)]
        self.assertEqual(self.r.album("Rumours", "Fleetwood Mac")["mbid"], R1)
        self.assertIs(self.r.lidarr, lidarr)            # one failure: Lidarr is asked again next time
        self.r.album("Rumours", "Fleetwood Mac")
        self.r.album("Rumours", "Fleetwood Mac")
        self.assertIsNone(self.r.lidarr)                 # three in a row: MusicBrainz for the rest

    def test_a_search_lidarr_cannot_answer_is_not_an_outage(self):
        # Real: "Search for 'Jungle Sunshine' failed. Invalid response received from LidarrAPI." (503)
        self.r.lidarr.unanswerable = {"Jungle Sunshine", "Bonobo Distance in Static", "X Y"}
        self.mb.groups[("Sunshine", "Jungle")] = [rg(R1, "Sunshine", "Jungle", A1, year="2026")]
        for title, artist in (("Sunshine", "Jungle"), ("Distance in Static", "Bonobo"), ("Y", "X")):
            self.r.album(title, artist)
        self.assertIsNotNone(self.r.lidarr)              # still used for everything else
        self.assertEqual(self.r.album("Sunshine", "Jungle")["mbid"], R1)   # MusicBrainz decided that one

    def test_artists_by_exact_name(self):
        self.r.lidarr.artists["Star"] = [{"artistName": "Star Wars Orchestra", "foreignArtistId": A2},
                                         {"artistName": "Star", "foreignArtistId": A1}]
        self.assertEqual(self.r.artist("Star")["mbid"], A1)
        self.mb.artists["Nova"] = [artist_hit("Nova", A3)]
        self.assertEqual(self.r.artist("Nova")["mbid"], A3)   # Lidarr has none: MusicBrainz

    def test_a_song_goes_to_the_earliest_studio_album_it_is_on(self):
        deezer = {"data": [
            {"title_short": "Come As You Are", "artist": {"name": "Nirvana"}, "album": {"title": "MTV Unplugged In New York"}},
            {"title_short": "Come As You Are", "artist": {"name": "Nirvana"}, "album": {"title": "Live at Reading"}},
            {"title_short": "Come As You Are", "artist": {"name": "Nirvana"}, "album": {"title": "Nevermind (30th Anniversary Super Deluxe)"}},
            {"title_short": "Come As You Are", "artist": {"name": "Nirvana"}, "album": {"title": "Nevermind"}},
            {"title_short": "Come As You Are", "artist": {"name": "A Tribute"}, "album": {"title": "Covers"}},
            {"title_short": "Lithium", "artist": {"name": "Nirvana"}, "album": {"title": "Other"}}]}
        self.r.lidarr.albums["Nirvana Nevermind"] = [lidarr_album(R1, "Nevermind", "Nirvana", A1, date="1991-09-24")]
        with mock.patch.object(self.discover, "_get_json", return_value=deezer), \
                mock.patch.object(self.discover, "resolve_song") as musicbrainz:
            got = self.r.song("Come As You Are", "Nirvana")
        self.assertEqual(got["album"]["mbid"], R1)
        musicbrainz.assert_not_called()
        # Live albums are skipped without a lookup; "Nevermind" is looked up once.
        self.assertEqual(self.r.lidarr.calls, ["Nirvana Nevermind"])

    def test_an_exports_album_name_first_and_musicbrainz_last(self):
        self.r.lidarr.albums["Fleetwood Mac Rumours"] = [lidarr_album(R1, "Rumours", "Fleetwood Mac", A1)]
        with mock.patch.object(self.discover, "_get_json") as deezer:
            got = self.r.song("Dreams - 2004 Remaster", "Fleetwood Mac", album="Rumours (Super Deluxe)")
        self.assertEqual(got["album"]["mbid"], R1)
        deezer.assert_not_called()
        with mock.patch.object(self.discover, "_get_json", return_value={"data": []}), \
                mock.patch.object(self.discover, "resolve_song", return_value={"reason": "only on singles"}) as mbz:
            self.assertEqual(self.r.song("B-side", "Fleetwood Mac"), {"reason": "only on singles"})
        mbz.assert_called_once()


class TestChartParsing(unittest.TestCase):
    def test_apple_charts(self):
        from services.music import discover
        v2 = {"feed": {"results": [{"name": "Patient Zero", "artistName": "Taylor Swift", "releaseDate": "2026-09-24",
                                    "artworkUrl100": "https://is1.mzstatic.com/a/100x100bb.jpg",
                                    "genres": [{"name": "Pop"}, {"name": "Music"}]}]}}
        with mock.patch.object(discover, "_get_json", return_value=v2):
            [song] = discover.apple_chart("ca", "songs")
        self.assertEqual((song["rank"], song["genres"], song["artwork"]),
                         (1, ["Pop"], "https://is1.mzstatic.com/a/300x300bb.jpg"))
        legacy = {"feed": {"entry": {"im:name": {"label": "Second Song"}, "im:artist": {"label": "Neil Young"},
                                     "im:releaseDate": {"label": "2026-09-18T00:00:00-07:00"},
                                     "im:image": [{"label": "https://x/55x55bb.png"}, {"label": "https://x/170x170bb.png"}],
                                     "category": {"attributes": {"label": "Rock"}}}}}
        with mock.patch.object(discover, "_get_json", return_value=legacy):
            [album] = discover.itunes_genre_albums("ca", 21)   # a chart of one arrives as an object
        self.assertEqual((album["title"], album["date"], album["genres"], album["artwork"]),
                         ("Second Song", "2026-09-18", ["Rock"], "https://x/300x300bb.jpg"))


class _DiscoverBase(_Base):
    def setUp(self):
        super().setUp()
        from services.music import discover
        discover.jobs.update(trending=False, all_time=False)
        discover._failed.update(trending=0.0, all_time=0.0)
        self.addCleanup(lambda: discover.jobs.update(trending=False, all_time=False))
        from services.music import spotify
        spotify._active.clear()
        self.addCleanup(spotify._active.clear)
        # Never the network: song preparation (Deezer + MusicBrainz batches) is tested on its own.
        p = mock.patch.object(discover.Resolver, "prepare_songs", lambda resolver, songs: None)
        p.start()
        self.addCleanup(p.stop)
        self.fmb = FakeMB()


class TestBuildTrending(_DiscoverBase):
    def test_sections_from_the_charts(self):
        from models.database import MusicArtist
        from services.music import discover
        today = date.today()
        fresh, old = (today - timedelta(days=5)).isoformat(), (today - timedelta(days=200)).isoformat()
        soon = (today + timedelta(days=10)).isoformat()
        songs = [{"rank": 1, "title": "Hit (feat. B)", "artist": "Star & Friend", "date": fresh, "artwork": "s1",
                  "genres": ["Pop"]},
                 {"rank": 2, "title": "Hit - Radio Edit", "artist": "Star & Friend", "date": fresh, "artwork": "s1",
                  "genres": ["Pop"]},   # the same song again: listed once
                 {"rank": 3, "title": "Lonely", "artist": "Nobody", "date": fresh, "artwork": "", "genres": []}]
        albums = [{"rank": 1, "title": "Shiny", "artist": "Star", "date": fresh, "artwork": "art", "genres": ["Pop"]},
                  {"rank": 2, "title": "Classic", "artist": "Oldie", "date": old, "artwork": "", "genres": ["Rock"]}]
        rock = [{"rank": 1, "title": "Shiny", "artist": "Star", "date": fresh, "artwork": "art", "genres": ["Rock"]}]
        self.fmb.artists["Star"] = [artist_hit("Star", A1)]
        self.fmb.groups[("Shiny", "Star")] = [rg(R1, "Shiny", "Star", A1, year=fresh[:4])]
        self.fmb.rgs[R3] = rg(R3, "Next", "Mine", A3, year=soon[:4])
        self.fmb.rgs[R4] = rg(R4, "Live Now", "Mine", A3, secondary=["Live"])
        self.db.add(MusicArtist(mbid=A3, name="Mine"))
        self.db.commit()
        fresh_releases = [{"rgid": R3, "title": "Next", "artist": "Mine", "artist_mbids": [A3], "date": soon},
                          {"rgid": R4, "title": "Live Now", "artist": "Mine", "artist_mbids": [A3], "date": fresh},
                          {"rgid": R2, "title": "Other", "artist": "Stranger", "artist_mbids": [A2], "date": fresh}]

        def chart(country, kind, limit=100):
            return songs if kind == "songs" else albums

        def genre_chart(country, gid, limit=100):
            if gid == 1153:
                raise discover.requests.ConnectionError("down")
            return rock if gid == 21 else []

        def song(title, artist, album="", album_artist=""):
            if artist == "Star & Friend":
                return {"album": {"mbid": R1, "title": "Shiny", "year": fresh[:4], "artist": "Star", "artist_mbid": A1}}
            return {"reason": "nope"}
        with mock.patch.object(discover, "apple_chart", side_effect=chart), \
                mock.patch.object(discover, "itunes_genre_albums", side_effect=genre_chart), \
                mock.patch.object(discover, "lb_fresh_releases", return_value=fresh_releases), \
                mock.patch.object(discover, "_deezer_picture", return_value="pic"):
            resolver = discover.Resolver(self.db, self.fmb)
            resolver.lidarr, resolver.deezer = None, False   # MusicBrainz (faked) decides here
            with mock.patch.object(resolver, "song", side_effect=song):
                t = discover.build_trending(self.db, resolver, "ca")
        # "Star & Friend" and "Star" add up; the whole credit isn't an artist, "Star" is.
        self.assertEqual([(a["name"], a["picture"]) for a in t["artists"]], [("Star", "pic")])
        self.assertEqual([(s["title"], s["album"]["mbid"]) for s in t["songs"]], [("Hit", R1)])
        [release] = t["releases"]   # the 200-day-old album isn't new
        self.assertEqual((release["mbid"], release["cover"], release["genres"]), (R1, "art", ["Pop", "Rock"]))
        # From your artists: studio albums only, announced ones flagged.
        self.assertEqual([(y["mbid"], y.get("upcoming")) for y in t["yours"]], [(R3, True)])
        self.assertEqual(t["errors"], ["Apple Music's Metal chart"])


class TestBuildAllTime(_DiscoverBase):
    def test_most_listened_studio_albums_by_genre(self):
        from services.music import discover
        from services.musicbrainz import MusicBrainzError
        page = [{"release_group_mbid": R1, "listen_count": 900}, {"release_group_mbid": R1, "listen_count": 800},
                {"release_group_mbid": R2, "listen_count": 700}, {"release_group_mbid": None},
                {"release_group_mbid": R3, "listen_count": 600}, {"release_group_mbid": R4, "listen_count": 500}]
        self.fmb.rgs[R1] = rg(R1, "OK Computer", "Radiohead", A1, genres=[("alternative rock", 25), ("rock", 13)])
        self.fmb.rgs[R2] = rg(R2, "Hits", "Band", A2, secondary=["Compilation"], genres=[("rock", 5)])
        self.fmb.rgs[R3] = MusicBrainzError("gone", 404)
        self.fmb.rgs[R4] = rg(R4, "Kind of Blue", "Miles Davis", A3, genres=[("modal jazz", 9), ("jazz", 9)])
        progress = []
        with mock.patch.object(discover, "lb_top_release_groups", side_effect=[page]), \
                mock.patch.object(discover, "PER_GENRE", 5):
            result = discover.build_all_time(self.db, self.fmb, lambda g, done, total: progress.append((done, total)))
        self.assertEqual({k: [a["title"] for a in v] for k, v in result["genres"].items()},
                         {"Rock": ["OK Computer"], "Jazz": ["Kind of Blue"]})
        self.assertEqual(result["genres"]["Rock"][0]["listens"], 900)
        self.assertEqual(result["checked"], 4)


class TestFreshness(_DiscoverBase):
    def test_stale_sections_are_queued_once_and_failures_back_off(self):
        from services.music import discover
        discover.ensure_fresh(self.db)
        self.assertEqual(len(self.jobs), 3)            # trending, all-time, lists
        discover.ensure_fresh(self.db)
        self.assertEqual(len(self.jobs), 3)            # already queued: not again
        with mock.patch.object(discover, "build_trending", side_effect=discover.MusicBrainzError("no contact")), \
                mock.patch.object(discover, "build_all_time", return_value={"built": time.time(), "genres": {}}), \
                mock.patch.object(discover, "build_lists",
                                  return_value={"built": time.time(), "ids": discover.list_ids(self.db), "lists": []}):
            with self.assertRaises(discover.MusicBrainzError):
                self.jobs.pop(0)(self.db)
            self.jobs.pop(0)(self.db)
            self.jobs.pop(0)(self.db)
        self.assertEqual(discover.load(self.db)["trending_error"], "no contact")
        discover.ensure_fresh(self.db)
        self.assertEqual(self.jobs, [])                # a failure isn't retried on every page view
        discover.ensure_fresh(self.db, force=True)
        self.assertEqual(len(self.jobs), 3)

    def test_a_new_chart_country_rebuilds_trending(self):
        from models.database import set_setting
        from services.music import discover
        discover._save(self.db, trending={"country": "us", "built": time.time()},
                       all_time={"built": time.time(), "genres": {"Rock": []}},
                       lists={"built": time.time(), "ids": discover.list_ids(self.db), "lists": []})
        discover.ensure_fresh(self.db)
        self.assertEqual(self.jobs, [])
        set_setting(self.db, "music_chart_country", "ca")
        discover.ensure_fresh(self.db)
        self.assertEqual(len(self.jobs), 1)


class TestPage(_DiscoverBase):
    def test_cards_carry_their_library_status(self):
        from models.database import MusicAlbum, set_setting
        from services.music import discover
        set_setting(self.db, "music_chart_country", "ca")
        card = {"mbid": R1, "title": "Shiny", "artist": "Star", "artist_mbid": A1, "year": "2026", "cover": "c"}
        discover._save(self.db, trending={"country": "ca", "built": time.time(), "artists": [], "releases": [card],
                                          "yours": [], "songs": [{"title": "Hit", "artist": "Star", "artist_mbid": A1,
                                                                  "artwork": "", "album": dict(card)}]},
                       all_time={"built": time.time(), "genres": {"Pop": [dict(card, mbid=R2)]}},
                       lists={"built": time.time(), "ids": discover.list_ids(self.db),
                              "lists": [{"id": "l1", "name": "Best", "albums": [dict(card, rank=1)]}]})
        self.db.add(MusicAlbum(mbid=R1, title="Shiny", monitored=True, track_count=10, track_file_count=10))
        self.db.commit()
        page = discover.page(self.db, self.user)
        self.assertEqual(self.jobs, [])
        self.assertEqual(page["new"]["releases"][0]["status"], "in_library")
        self.assertEqual(page["trending"]["songs"][0]["album"]["status"], "in_library")
        self.assertEqual(page["all_time"]["genres"], [])   # one album isn't worth a genre tab
        with mock.patch.object(discover, "MIN_GENRE_ALBUMS", 1):
            page = discover.page(self.db, self.user)
        self.assertEqual(page["all_time"]["genres"][0]["albums"][0]["status"], "available")
        self.assertEqual(page["country"], "ca")
        self.assertEqual(page["lists"]["items"], [{"id": "l1", "name": "Best", "count": 1}])
        self.assertEqual(discover.list_page(self.db, "l1")["albums"][0]["status"], "in_library")
        self.assertIsNone(discover.list_page(self.db, "nope"))


EMBED = """<html><script id="__NEXT_DATA__" type="application/json">%s</script></html>"""


def embed_page(name, tracks):
    return EMBED % json.dumps({"props": {"pageProps": {"state": {"data": {"entity": {
        "name": name, "trackList": [dict(t, entityType="track") for t in tracks]}}}}}})


class TestSpotifyReading(unittest.TestCase):
    def test_playlist_links(self):
        from services.music.spotify import SpotifyImportError, playlist_id
        self.assertEqual(playlist_id("https://open.spotify.com/playlist/37i9dQZF1DWXRqgorJj26U?si=abc"),
                         "37i9dQZF1DWXRqgorJj26U")
        self.assertEqual(playlist_id("spotify:playlist:37i9dQZF1DWXRqgorJj26U"), "37i9dQZF1DWXRqgorJj26U")
        with self.assertRaises(SpotifyImportError):
            playlist_id("https://open.spotify.com/album/37i9dQZF1DWXRqgorJj26U")

    def test_the_embed_page(self):
        from services.music import spotify
        page = embed_page("Rock Classics", [{"title": "Dreams - 2004 Remaster", "subtitle": "Fleetwood Mac"},
                                            {"title": "Gold", "subtitle": "A, B"}])
        resp = mock.Mock(status_code=200, text=page)
        with mock.patch.object(spotify.requests, "get", return_value=resp):
            name, songs = spotify.fetch_playlist("https://open.spotify.com/playlist/37i9dQZF1DWXRqgorJj26U")
        self.assertEqual((name, songs[1]["artist"]), ("Rock Classics", "A, B"))
        missing = EMBED % json.dumps({"props": {"pageProps": {"status": 404, "title": "Page not found"}}})
        for status, text, want in ((404, "", "isn't public"), (200, missing, "isn't public"),
                                   (200, "<html>new design</html>", "Exportify")):
            with mock.patch.object(spotify.requests, "get", return_value=mock.Mock(status_code=status, text=text)):
                with self.assertRaises(spotify.SpotifyImportError) as e:
                    spotify.fetch_playlist("https://open.spotify.com/playlist/37i9dQZF1DWXRqgorJj26U")
            self.assertIn(want, e.exception.message)

    def test_exportify_files_old_and_new(self):
        from services.music.spotify import SpotifyImportError, parse_exportify
        new = ("﻿Track URI,Track Name,Album Name,Artist Name(s),Album Artist Name(s),ISRC\n"
               'spotify:track:1,Dreams - 2004 Remaster,Rumours (Super Deluxe),Fleetwood Mac,Fleetwood Mac,US1\n'
               'spotify:track:2,"Ohio","4 Way Street","Crosby, Stills, Nash & Young",x,US2\n').encode()
        name, songs = parse_exportify(new, "my_road_trip.csv")
        self.assertEqual(name, "my road trip")
        self.assertEqual(songs[0], {"title": "Dreams - 2004 Remaster", "artist": "Fleetwood Mac",
                                    "album": "Rumours (Super Deluxe)", "album_artist": "Fleetwood Mac"})
        self.assertEqual(songs[1]["artist"], "Crosby, Stills, Nash & Young")
        old = b"Spotify URI,Track Name,Artist Name,Album Name\nx,Song,Someone,Record\n"
        self.assertEqual(parse_exportify(old)[1][0]["artist"], "Someone")
        with self.assertRaises(SpotifyImportError):
            parse_exportify(b"a,b\n1,2\n")


class TestSpotifyImport(_DiscoverBase):
    SONGS = [{"title": "Dreams", "artist": "Fleetwood Mac"}, {"title": "Go Your Own Way", "artist": "Fleetwood Mac"},
             {"title": "dreams", "artist": "fleetwood mac"},     # a duplicate
             {"title": "Rare Single", "artist": "Band"}]

    def _resolve(self, title, artist, album="", album_artist=""):
        if artist.lower() == "fleetwood mac":
            return {"album": {"mbid": R1, "title": "Rumours", "year": "1977", "artist": "Fleetwood Mac",
                              "artist_mbid": A1}}
        return {"reason": "only on singles"}

    def test_import_preview_and_requests(self):
        from services.media_requests import RequestRefused
        from services.music import discover, spotify
        imp = spotify.start_import(self.db, self.user.id, "Road trip", "spotify_url", "u", self.SONGS)
        self.assertEqual(imp.total, 3)
        with mock.patch.object(discover.Resolver, "song", side_effect=self._resolve):
            self.run_jobs()
        self.db.refresh(imp)
        self.assertEqual((imp.status, imp.done), ("ready", 3))
        albums, skipped = spotify.albums_of(imp)
        self.assertEqual([(a["title"], a["songs"]) for a in albums], [("Rumours", ["Dreams", "Go Your Own Way"])])
        self.assertEqual([s["title"] for s in skipped], ["Rare Single"])
        self.assertEqual(spotify.albums_for_discover(self.db, self.user)[0]["playlists"], ["Road trip"])

        calls = []

        def fake_request(db, rgid, **kw):
            calls.append((rgid, kw["via"]))
            if rgid == R2:
                raise RequestRefused("Lidarr's metadata server doesn't know this album yet.")
            return {}
        with mock.patch("services.media_requests.request_album", side_effect=fake_request):
            spotify.request_job(imp.id, [R1, R2], self.user.id)(self.db)
        self.assertEqual([c[0] for c in calls], [R1, R2])
        self.assertIn("Road trip", calls[0][1])
        self.db.refresh(imp)
        self.assertEqual(imp.outcomes[R1], "requested")
        self.assertIn("metadata server", imp.outcomes[R2])

    def test_a_long_playlist_is_resolved_in_turns(self):
        from services.music import discover, spotify
        songs = [{"title": f"Song {i}", "artist": "Fleetwood Mac"} for i in range(25)]
        imp = spotify.start_import(self.db, self.user.id, "Long", "exportify_csv", "", songs)
        turns = 0
        with mock.patch.object(discover.Resolver, "song", side_effect=self._resolve), \
                mock.patch.object(spotify, "CHUNK", 10):
            while self.jobs:
                self.jobs.pop(0)(self.db)
                turns += 1
        self.db.refresh(imp)
        self.assertEqual((turns, imp.done, imp.status), (3, 25, "ready"))   # 10 + 10 + 5
        self.assertNotIn(imp.id, spotify._active)

    def test_refresh_keeps_resolved_songs_and_resumes(self):
        from services.music import discover, spotify
        imp = spotify.start_import(self.db, self.user.id, "Road trip", "spotify_url",
                                   "https://open.spotify.com/playlist/37i9dQZF1DWXRqgorJj26U", self.SONGS[:1])
        with mock.patch.object(discover.Resolver, "song", side_effect=self._resolve) as resolve:
            self.run_jobs()
            with mock.patch.object(spotify, "fetch_playlist", return_value=("Road trip 2", self.SONGS[:2])):
                spotify.refresh_import(self.db, imp)
            self.run_jobs()
        self.assertEqual(resolve.call_count, 2)        # only the new song was looked up
        self.db.refresh(imp)
        self.assertEqual((imp.name, imp.done, imp.total, imp.status), ("Road trip 2", 2, 2, "ready"))
        csv_import = spotify.start_import(self.db, self.user.id, "CSV", "exportify_csv", "", self.SONGS[:1])
        with self.assertRaises(spotify.SpotifyImportError):
            spotify.refresh_import(self.db, csv_import)

    def test_a_musicbrainz_outage_pauses_the_import_with_the_reason_and_it_carries_on(self):
        # #248: a 503 used to stop an import at "error" for good.
        from services.music import discover, spotify
        later = []
        imp = spotify.start_import(self.db, self.user.id, "P", "exportify_csv", "", self.SONGS)
        with mock.patch.object(discover.Resolver, "song", side_effect=discover.MusicBrainzError("rate limited", 503)), \
                mock.patch.object(spotify, "_later", side_effect=lambda delay, fn: later.append(fn)):
            self.run_jobs()
        self.db.refresh(imp)
        self.assertEqual(imp.status, "resolving")
        self.assertIn("rate limited", imp.error)
        spotify.summaries(self.db, self.user)
        self.assertEqual(self.jobs, [])            # a page poll doesn't hammer MusicBrainz meanwhile
        [retry] = later
        retry()                                     # the back-off is over
        with mock.patch.object(discover.Resolver, "song", side_effect=self._resolve):
            self.run_jobs()
        self.db.refresh(imp)
        self.assertEqual((imp.status, imp.done, imp.error), ("ready", 3, None))

    def test_a_setting_to_fix_stops_the_import_and_retry_carries_on(self):
        from services.music import discover, spotify
        imp = spotify.start_import(self.db, self.user.id, "P", "exportify_csv", "", self.SONGS)
        no_contact = discover.MusicBrainzError("Set a contact email", transient=False)
        with mock.patch.object(discover.Resolver, "song", side_effect=no_contact):
            self.run_jobs()
        self.db.refresh(imp)
        self.assertEqual((imp.status, imp.error), ("error", "MusicBrainz: Set a contact email"))
        spotify.retry_import(self.db, imp)
        with mock.patch.object(discover.Resolver, "song", side_effect=self._resolve):
            self.run_jobs()
        self.db.refresh(imp)
        self.assertEqual((imp.status, imp.done), ("ready", 3))

    def test_imports_are_private_to_their_owner(self):
        import models.database as mdb
        from services.music import spotify
        other = mdb.TentacleUser(id=2, jellyfin_user_id="u2", display_name="v", is_admin=False)
        self.db.add(other)
        self.db.commit()
        imp = spotify.start_import(self.db, self.user.id, "Mine", "exportify_csv", "", self.SONGS[:1])
        with self.assertRaises(spotify.SpotifyImportError):
            spotify.get_import(self.db, other, imp.id)
        self.assertEqual(spotify.summaries(self.db, other), [])


class TestImportEndpoints(_DiscoverBase):
    def setUp(self):
        super().setUp()
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        import routers.music as music
        from models.database import get_db
        from routers.auth import get_user_from_request, require_admin
        app = FastAPI()
        app.include_router(music.router)
        app.include_router(music.webhook_router)
        app.dependency_overrides[get_db] = lambda: self.Session()
        app.dependency_overrides[require_admin] = lambda: self.user
        app.dependency_overrides[get_user_from_request] = lambda: self.user
        self.client = TestClient(app)

    def test_link_and_file_imports(self):
        from services.music import discover, spotify
        with mock.patch.object(spotify, "fetch_playlist", return_value=("Linked", TestSpotifyImport.SONGS[:2])):
            r = self.client.post("/api/music/imports", data={"url": "https://open.spotify.com/playlist/x"})
        self.assertEqual((r.status_code, r.json()["total"]), (200, 2))
        csv_data = b"Track Name,Artist Name(s)\nDreams,Fleetwood Mac\n"
        r = self.client.post("/api/music/imports", files={"file": ("list.csv", io.BytesIO(csv_data), "text/csv")})
        self.assertEqual(r.json()["name"], "list")
        self.assertEqual(self.client.post("/api/music/imports", data={}).status_code, 400)
        with mock.patch.object(discover.Resolver, "song", side_effect=TestSpotifyImport._resolve.__get__(self)):
            self.run_jobs()
        imports = self.client.get("/api/music/imports").json()["imports"]
        detail = self.client.get(f"/api/music/imports/{imports[0]['id']}").json()
        self.assertEqual([(a["mbid"], a["status"]) for a in detail["albums"]], [(R1, "available")])
        # Only albums of this playlist can be requested from it.
        r = self.client.post(f"/api/music/imports/{detail['id']}/request", json={"mbids": [R2]})
        self.assertEqual(r.status_code, 400)
        r = self.client.post(f"/api/music/imports/{detail['id']}/request", json={"mbids": [R1, R1]})
        self.assertEqual(r.json(), {"queued": 1})
        self.assertEqual(self.client.delete(f"/api/music/imports/{detail['id']}").json(), {"deleted": True})
        self.assertEqual(self.client.get(f"/api/music/imports/{detail['id']}").status_code, 404)

    def test_discover_answers_from_the_cache(self):
        from services.music import discover
        discover._save(self.db, trending={"country": "us", "built": time.time()},
                       all_time={"built": time.time(), "genres": {}})
        r = self.client.get("/api/music/discover")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(set(r.json()), {"country", "building", "errors", "trending", "new", "all_time", "spotify",
                                         "lists"})
        self.assertEqual(self.client.post("/api/music/discover/refresh").json(), {"queued": True})


class TestMusicBrainzTurns(unittest.TestCase):
    """One request a second for the whole app: pages go before the worker's lookups."""

    def setUp(self):
        import services.musicbrainz as mbmod
        self.mbmod = mbmod
        self.starts = []

        def fake_get(url, **kw):
            import threading as th
            self.starts.append((th.current_thread().name, time.monotonic()))
            time.sleep(self.latency)
            return mock.Mock(status_code=200, json=lambda: {"ok": True})
        self.latency = 0.0
        for patch in (mock.patch.object(mbmod.requests, "get", side_effect=fake_get),
                      mock.patch.object(mbmod, "MIN_INTERVAL", 0.05),
                      mock.patch.object(mbmod, "INTERACTIVE_GRACE", 0.4)):
            patch.start()
            self.addCleanup(patch.stop)
        mbmod._last_request[0] = 0.0
        mbmod._interactive.update(waiting=0, last=0.0)

    def call(self):
        return self.mbmod.get("/artist/x", contact="me@example.com")

    def test_the_interval_runs_from_one_start_to_the_next(self):
        self.latency = 0.1
        with mock.patch.object(self.mbmod, "MIN_INTERVAL", 0.15):
            for _ in range(4):
                self.call()
        gaps = [b[1] - a[1] for a, b in zip(self.starts, self.starts[1:])]
        # 0.15 from one start to the next, not 0.15 after each answer (0.25).
        self.assertTrue(all(0.14 <= g < 0.21 for g in gaps), gaps)

    def test_a_page_goes_before_the_worker_and_keeps_its_turn(self):
        import threading
        stop = threading.Event()

        def worker_loop():
            while not stop.is_set():
                self.call()
        bg = threading.Thread(target=worker_loop, name="music-worker")
        bg.start()
        time.sleep(0.2)
        for _ in range(3):      # an album page: a few lookups in a row
            self.call()
            time.sleep(0.02)
        page_done = time.monotonic()
        time.sleep(0.6)
        stop.set()
        bg.join()
        names = [n for n, _ in self.starts]
        first = names.index("MainThread")
        self.assertEqual(names[first:first + 3], ["MainThread"] * 3)      # nothing in between
        resumed = [t for n, t in self.starts if n == "music-worker" and t > page_done]
        self.assertTrue(resumed and resumed[0] - page_done >= 0.3)       # the worker waited out the grace


class TestBatches(unittest.TestCase):
    """MusicBrainz allows one request a second: names are matched eight per request."""

    def setUp(self):
        from services.music import discover
        self.discover = discover
        self.mb = FakeMB()
        self.r = discover.Resolver.__new__(discover.Resolver)
        self.r.mb, self.r.lidarr, self.r.deezer = self.mb, None, False
        self.r.reset()

    def test_eight_albums_per_request_and_answers_kept(self):
        for i in range(10):
            self.mb.groups[(f"Album {i}", f"Artist {i}")] = [rg(f"{i:08d}-0000-0000-0000-000000000000", f"Album {i}",
                                                                 f"Artist {i}", A1)]
        self.r.prefetch([(f"Album {i} (Deluxe)", f"Artist {i}", "") for i in range(10)])
        self.assertEqual(self.mb.searches, 2)            # 8 + 2
        with mock.patch.object(self.discover, "find_album") as single:
            self.assertEqual(self.r.album("Album 3", "Artist 3")["title"], "Album 3")
        single.assert_not_called()                       # answered from the batch

    def test_same_titled_albums_nearest_to_the_chart_date(self):
        # Weezer has six studio albums called "Weezer".
        self.mb.groups[("Weezer", "Weezer")] = [rg(R1, "Weezer", "Weezer", A1, year="1994"),
                                                rg(R2, "Weezer", "Weezer", A1, year="2016"),
                                                rg(R3, "Weezer", "Weezer", A1, year="2026")]
        self.r.prefetch([("Weezer", "Weezer", "2026-09-19"), ("Weezer", "Weezer", "")])
        self.assertEqual(self.r.album("Weezer", "Weezer", near="2026-09-19")["mbid"], R3)
        self.assertEqual(self.r.album("Weezer", "Weezer")["mbid"], R1)      # no date: the earliest

    def test_an_album_pushed_out_of_a_capped_batch_is_asked_again(self):
        # #251: a batch's results are cut at 100; an album that ranked below the cut
        # was taken as "not on MusicBrainz" and silently dropped from New releases.
        import random
        seed = random.randrange(1 << 30)
        pushed_out = random.Random(seed).randrange(8)
        wanted = [(f"Album {i}", f"Artist {i}", "") for i in range(8)]
        for i in range(8):
            self.mb.groups[(f"Album {i}", f"Artist {i}")] = [rg(f"{i:08d}-0000-0000-0000-000000000000",
                                                                 f"Album {i}", f"Artist {i}", A1)]
        real = self.mb.find_release_groups_batch
        filler = [rg(f"ffffffff-0000-0000-0000-{n:012d}", f"Album {pushed_out} Tribute {n}", "Cover Band", A2,
                     score=90) for n in range(100)]

        def capped(items):
            out = real(items)
            if len(items) > 1:   # longer titles and other artists crowd out the real match
                out = [g for g in out if g["title"] != f"Album {pushed_out}"]
                out = (out + filler)[:100]
            return out
        with mock.patch.object(self.mb, "find_release_groups_batch", side_effect=capped):
            self.r.prefetch(wanted)
        with mock.patch.object(self.discover, "find_album", return_value=None) as single:
            card = self.r.album(f"Album {pushed_out}", f"Artist {pushed_out}")
        self.assertIsNotNone(card, f"seed={seed}")
        self.assertEqual(card["title"], f"Album {pushed_out}")
        single.assert_not_called()                       # found by the smaller batches
        self.assertIsNotNone(self.r.album("Album 0" if pushed_out else "Album 1",
                                          "Artist 0" if pushed_out else "Artist 1"))

    def test_an_album_capped_on_its_own_is_left_to_the_one_album_lookup(self):
        filler = [rg(f"ffffffff-0000-0000-0000-{n:012d}", f"Hello {n}", "Adele Tribute", A2) for n in range(100)]
        with mock.patch.object(self.mb, "find_release_groups_batch", return_value=filler):
            self.r.prefetch([("Hello", "Adele", "")])
        with mock.patch.object(self.discover, "find_album", return_value=rg(R1, "Hello", "Adele", A1)) as single:
            self.assertEqual(self.r.album("Hello", "Adele")["mbid"], R1)
        single.assert_called_once()

    def test_a_title_that_is_only_a_single_is_not_an_album(self):
        self.mb.groups[("Hit", "Star")] = [rg(R1, "Hit", "Star", A1, kind="Single")]
        self.r.prefetch([("Hit", "Star", "")])
        with mock.patch.object(self.discover, "find_album") as single:
            self.assertIsNone(self.r.album("Hit", "Star"))
        single.assert_not_called()

    def test_songs_are_prepared_in_batches(self):
        deezer = {"data": [{"title_short": "Dreams", "artist": {"name": "Fleetwood Mac"}, "album": {"title": "Rumours"}},
                           {"title_short": "Go Your Own Way", "artist": {"name": "Fleetwood Mac"},
                            "album": {"title": "Rumours"}}]}
        self.r.deezer = True
        self.mb.groups[("Rumours", "Fleetwood Mac")] = [rg(R1, "Rumours", "Fleetwood Mac", A1)]
        with mock.patch.object(self.discover, "_get_json", return_value=deezer):
            self.r.prepare_songs([("Dreams", "Fleetwood Mac", "", ""), ("Go Your Own Way", "Fleetwood Mac", "", "")])
            self.assertEqual(self.mb.searches, 1)
            with mock.patch.object(self.discover, "resolve_song") as recordings:
                self.assertEqual(self.r.song("Dreams", "Fleetwood Mac")["album"]["mbid"], R1)
            recordings.assert_not_called()

    def test_songs_are_placed_by_their_recordings_four_per_request(self):
        # The songs' own recordings decide (the rule Song search uses); the artist ids
        # come from Lidarr, so MusicBrainz is asked once for all of them.
        def recording(title, artist_id, rgid, album, date, kind="Album", secondary=()):
            return {"id": f"rec-{title}", "title": title, "artist-credit": [{"artist": {"id": artist_id}}],
                    "releases": [{"status": "Official", "date": date, "artist-credit": [{"artist": {"id": artist_id}}],
                                  "release-group": {"id": rgid, "title": album, "primary-type": kind,
                                                    "secondary-types": list(secondary)}}]}
        self.r.lidarr = FakeLidarrLookups()
        self.r.lidarr.artists["Ella Langley"] = [{"artistName": "Ella Langley", "foreignArtistId": A1}]
        self.mb.recordings = {
            ("Choosin' Texas", A1): [recording("Choosin' Texas", A1, R2, "Choosin' Texas", "2026-05-01", kind="Single"),
                                     recording("Choosin' Texas", A1, R1, "Dandelion", "2026-08-01")],
            ("Be Her", A1): [recording("Be Her", A1, R1, "Dandelion", "2026-08-01")]}
        self.r.prepare_songs([("Choosin' Texas", "Ella Langley", "", ""), ("Be Her", "Ella Langley", "", "")])
        self.assertEqual(self.mb.recording_searches, 1)
        with mock.patch.object(self.discover, "resolve_song") as single:
            for song in ("Choosin' Texas", "Be Her"):
                got = self.r.song(song, "Ella Langley")["album"]
                self.assertEqual((got["mbid"], got["title"], got["year"]), (R1, "Dandelion", "2026"))
        single.assert_not_called()

    def test_a_capped_recording_search_is_not_trusted(self):
        self.r.lidarr = FakeLidarrLookups()
        self.r.lidarr.artists["Fleetwood Mac"] = [{"artistName": "Fleetwood Mac", "foreignArtistId": A1}]
        live = {"id": "x", "title": "Dreams", "artist-credit": [{"artist": {"id": A1}}],
                "releases": [{"status": "Official", "date": "2004", "artist-credit": [{"artist": {"id": A1}}],
                              "release-group": {"id": R3, "title": "Later Album", "primary-type": "Album"}}]}
        self.mb.recordings = {("Dreams", A1): [live] * 100}   # the original may be past the cap
        self.r.prepare_songs([("Dreams", "Fleetwood Mac", "", "")])
        self.assertNotIn(self.r._song_key("Dreams", "Fleetwood Mac"), self.r._song_found)

    def test_a_capped_batch_is_split_until_it_fits(self):
        self.r.lidarr = FakeLidarrLookups()
        self.r.lidarr.artists["Band"] = [{"artistName": "Band", "foreignArtistId": A1}]
        def rec(title, rgid):
            return {"id": title, "title": title, "artist-credit": [{"artist": {"id": A1}}],
                    "releases": [{"status": "Official", "date": "1970", "artist-credit": [{"artist": {"id": A1}}],
                                  "release-group": {"id": rgid, "title": "LP", "primary-type": "Album"}}]}
        self.mb.recordings = {("Hit", A1): [rec("Hit", R1)] * 90, ("Deep Cut", A1): [rec("Deep Cut", R2)] * 20}
        self.r.prepare_songs([("Hit", "Band", "", ""), ("Deep Cut", "Band", "", "")])
        self.assertEqual(self.mb.recording_searches, 3)          # both (110, capped), then each alone
        self.assertEqual(self.r._song_found[self.r._song_key("Hit", "Band")]["mbid"], R1)
        self.assertEqual(self.r._song_found[self.r._song_key("Deep Cut", "Band")]["mbid"], R2)

    def test_names_and_medleys(self):
        from services.music.browse import same_song
        self.assertEqual(self.discover._bare("Derek & The Dominos"), self.discover._bare("Derek and the Dominos"))
        self.assertTrue(same_song("Black Magic Woman / Gypsy Queen", "black magic woman"))
        self.assertFalse(same_song("Hey Jude", "hey"))

    def test_store_subtitles_are_dropped_when_needed(self):
        self.assertEqual(self.discover._title_variants("The Life of a Showgirl: The Encore"),
                         ["The Life of a Showgirl: The Encore", "The Life of a Showgirl"])
        self.assertEqual(self.discover._title_variants("Stick Season (We'll All Be Here Forever)"),
                         ["Stick Season (We'll All Be Here Forever)", "Stick Season"])
        self.assertEqual(self.discover._title_variants("Rumours"), ["Rumours"])

    def test_day_numbers(self):
        self.assertLess(self.discover._days("1977"), self.discover._days("1977-02-04") + 400)
        self.assertEqual(self.discover._days(""), 10 ** 7)


class TestLists(_DiscoverBase):
    def test_a_list_in_its_order_with_artists_and_years(self):
        from services.music import discover

        def rel(rgid, title, number):
            return {"type": "part of", "attribute-values": {"number": str(number)},
                    "release_group": {"id": rgid, "title": title}}
        self.fmb.lists["s1"] = {"name": "Rolling Stone: 500", "type": "Release group series",
                                "relations": [rel(R2, "Pet Sounds", 2), rel(R1, "What's Going On", 1),
                                              rel(R3, "Live at Leeds", 3)]}
        self.fmb.rgs[R1] = rg(R1, "What's Going On", "Marvin Gaye", A1, year="1971")
        self.fmb.rgs[R2] = rg(R2, "Pet Sounds", "The Beach Boys", A2, year="1966")
        self.fmb.rgs[R3] = rg(R3, "Live at Leeds", "The Who", A3, secondary=["Live"], year="1970")
        got = discover.build_list(self.fmb, "s1")
        self.assertEqual([(a["rank"], a["title"], a["artist"], a["year"]) for a in got["albums"]],
                         [(1, "What's Going On", "Marvin Gaye", "1971"), (2, "Pet Sounds", "The Beach Boys", "1966"),
                          (3, "Live at Leeds", "The Who", "1970")])
        self.assertEqual(got["albums"][2].get("type"), "Live")   # kept: the list chose it
        self.assertEqual(self.fmb.batches, 1)                     # all details in one request

    def test_the_list_setting_accepts_links_and_ids(self):
        from models.database import set_setting
        from services.music import discover
        set_setting(self.db, "music_lists", f"https://musicbrainz.org/series/{R1}, {R2},junk")
        self.assertEqual(discover.list_ids(self.db), [R1, R2])

    def test_adding_and_removing_lists(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        import routers.music as music
        from models.database import get_db, get_setting, set_setting
        from routers.auth import require_admin
        from services.musicbrainz import MusicBrainz
        app = FastAPI()
        app.include_router(music.router)
        app.dependency_overrides[get_db] = lambda: self.Session()
        app.dependency_overrides[require_admin] = lambda: self.user
        client = TestClient(app)
        set_setting(self.db, "music_lists", R1)
        series = {R2: {"name": "1001 Albums", "type": "Release group series"},
                  R3: {"name": "Eurovision winners", "type": "Recording series"}}
        with mock.patch.object(MusicBrainz, "series", autospec=True, side_effect=lambda _mb, sid: series[sid]):
            r = client.post("/api/music/lists", json={"series": f"https://musicbrainz.org/series/{R2}"})
            self.assertEqual(r.json(), {"id": R2, "name": "1001 Albums"})
            self.assertEqual(client.post("/api/music/lists", json={"series": R3}).status_code, 400)
            self.assertEqual(client.post("/api/music/lists", json={"series": "not a link"}).status_code, 400)
        self.db.expire_all()
        self.assertEqual(get_setting(self.db, "music_lists"), f"{R1},{R2}")
        client.delete(f"/api/music/lists/{R1}")
        self.db.expire_all()
        self.assertEqual(get_setting(self.db, "music_lists"), R2)


if __name__ == "__main__":
    unittest.main()
