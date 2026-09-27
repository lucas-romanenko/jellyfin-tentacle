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
        self.artists, self.groups, self.rgs = {}, {}, {}

    def search_artists(self, q, limit=8):
        return self.artists.get(q, [])

    def find_release_groups(self, title, artist, limit=10):
        return self.groups.get((title, artist), [])

    def release_group(self, rgid):
        value = self.rgs[rgid]
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

            def recordings_by(self, title, ids, limit=100):
                return [{"id": "r1", "title": "Tennessee Whiskey",
                         "releases": [release(R1, "Life Goes On", "2012", "various"),
                                      release(R2, "Traveller", "2015", A1)]}]
        self.assertEqual(original_album_for_song(MB(), "Tennessee Whiskey", A1)["album"]["id"], R2)


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

        def song(mb, title, artist, album="", album_artist=""):
            if artist == "Star & Friend":
                return {"album": {"mbid": R1, "title": "Shiny", "year": fresh[:4], "artist": "Star", "artist_mbid": A1}}
            return {"reason": "nope"}
        with mock.patch.object(discover, "apple_chart", side_effect=chart), \
                mock.patch.object(discover, "itunes_genre_albums", side_effect=genre_chart), \
                mock.patch.object(discover, "lb_fresh_releases", return_value=fresh_releases), \
                mock.patch.object(discover, "resolve_song", side_effect=song), \
                mock.patch.object(discover, "_deezer_picture", return_value="pic"):
            t = discover.build_trending(self.db, self.fmb, "ca")
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
        self.assertEqual(len(self.jobs), 2)            # trending + all-time
        discover.ensure_fresh(self.db)
        self.assertEqual(len(self.jobs), 2)            # already queued: not again
        with mock.patch.object(discover, "build_trending", side_effect=discover.MusicBrainzError("no contact")), \
                mock.patch.object(discover, "build_all_time", return_value={"built": time.time(), "genres": {}}):
            with self.assertRaises(discover.MusicBrainzError):
                self.jobs.pop(0)(self.db)
            self.jobs.pop(0)(self.db)
        self.assertEqual(discover.load(self.db)["trending_error"], "no contact")
        discover.ensure_fresh(self.db)
        self.assertEqual(self.jobs, [])                # a failure isn't retried on every page view
        discover.ensure_fresh(self.db, force=True)
        self.assertEqual(len(self.jobs), 2)

    def test_a_new_chart_country_rebuilds_trending(self):
        from models.database import set_setting
        from services.music import discover
        discover._save(self.db, trending={"country": "us", "built": time.time()},
                       all_time={"built": time.time(), "genres": {"Rock": []}})
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
                       all_time={"built": time.time(), "genres": {"Pop": [dict(card, mbid=R2)]}})
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

    def _resolve(self, mb, title, artist, album="", album_artist=""):
        if artist.lower() == "fleetwood mac":
            return {"album": {"mbid": R1, "title": "Rumours", "year": "1977", "artist": "Fleetwood Mac",
                              "artist_mbid": A1}}
        return {"reason": "only on singles"}

    def test_import_preview_and_requests(self):
        from services.media_requests import RequestRefused
        from services.music import discover, spotify
        imp = spotify.start_import(self.db, self.user.id, "Road trip", "spotify_url", "u", self.SONGS)
        self.assertEqual(imp.total, 3)
        with mock.patch.object(discover, "resolve_song", side_effect=self._resolve):
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
        with mock.patch.object(discover, "resolve_song", side_effect=self._resolve):
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
        with mock.patch.object(discover, "resolve_song", side_effect=self._resolve) as resolve:
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

    def test_a_musicbrainz_outage_stops_the_import_with_the_reason(self):
        from services.music import discover, spotify
        imp = spotify.start_import(self.db, self.user.id, "P", "exportify_csv", "", self.SONGS)
        with mock.patch.object(discover, "resolve_song", side_effect=discover.MusicBrainzError("rate limited", 503)):
            self.run_jobs()
        self.db.refresh(imp)
        self.assertEqual(imp.status, "error")
        self.assertIn("rate limited", imp.error)

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
        with mock.patch.object(discover, "resolve_song", side_effect=TestSpotifyImport._resolve.__get__(self)):
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
        self.assertEqual(set(r.json()), {"country", "building", "errors", "trending", "new", "all_time", "spotify"})
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


if __name__ == "__main__":
    unittest.main()
